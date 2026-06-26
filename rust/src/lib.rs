//! Rust-accelerated L3 (market-by-order) core for convexpi-arena.
//!
//! A line-for-line port of the reference semantics in `convexpi/arena/mbo.py`:
//! `L3Book` (order-by-order book with FIFO queues) and `simulate_passive_order`
//! (queue position -> fill / cancel race / adverse selection). The Python module
//! remains the canonical definition; `tests/arena/test_rust_conformance.py`
//! asserts this implementation matches it field-for-field.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use std::collections::{HashMap, HashSet};

// --- event extracted from a Python dict ----------------------------------
// e: 0 = none/trade, 1 = created, 2 = changed, 3 = deleted
struct Ev {
    is_trade: bool,
    e: u8,
    id: i64,
    p: f64,
    a: f64,
    s: i64,
    t: i64,
}

#[inline]
fn key(p: f64) -> u64 {
    p.to_bits()
}

fn parse_one(d: &Bound<'_, PyDict>) -> PyResult<Ev> {
    let k: String = d.get_item("k")?.expect("event missing 'k'").extract()?;
    let is_trade = k == "t";
    let e: u8 = match d.get_item("e")? {
        Some(v) => match v.extract::<String>()?.as_str() {
            "created" => 1,
            "changed" => 2,
            "deleted" => 3,
            _ => 0,
        },
        None => 0,
    };
    let id: i64 = match d.get_item("id")? {
        Some(v) => v.extract()?,
        None => 0,
    };
    let p: f64 = d.get_item("p")?.expect("event missing 'p'").extract()?;
    let a: f64 = d.get_item("a")?.expect("event missing 'a'").extract()?;
    let s: i64 = d.get_item("s")?.expect("event missing 's'").extract()?;
    let t: i64 = d.get_item("t")?.expect("event missing 't'").extract()?;
    Ok(Ev { is_trade, e, id, p, a, s, t })
}

fn parse_events(events: &Bound<'_, PyList>) -> PyResult<Vec<Ev>> {
    let mut out = Vec::with_capacity(events.len());
    for item in events.iter() {
        let d = item.downcast::<PyDict>()?;
        out.push(parse_one(d)?);
    }
    Ok(out)
}

// --- the book ------------------------------------------------------------
struct Book {
    orders: HashMap<i64, (f64, f64, i64)>, // id -> (price, amount, side)
    levels: [HashMap<u64, Vec<i64>>; 2],   // side -> price_bits -> FIFO ids
}

impl Book {
    fn new() -> Self {
        Book { orders: HashMap::new(), levels: [HashMap::new(), HashMap::new()] }
    }

    fn apply_ev(&mut self, ev: &Ev) {
        match ev.e {
            1 => {
                self.orders.insert(ev.id, (ev.p, ev.a, ev.s));
                self.levels[ev.s as usize].entry(key(ev.p)).or_default().push(ev.id);
            }
            2 => {
                if let Some(o) = self.orders.get_mut(&ev.id) {
                    o.1 = ev.a;
                }
            }
            3 => {
                if let Some((price, _, side)) = self.orders.remove(&ev.id) {
                    let lvl = &mut self.levels[side as usize];
                    if let Some(lst) = lvl.get_mut(&key(price)) {
                        if let Some(pos) = lst.iter().position(|&x| x == ev.id) {
                            lst.remove(pos);
                        }
                        if lst.is_empty() {
                            lvl.remove(&key(price));
                        }
                    }
                }
            }
            _ => {}
        }
    }

    fn best_bid(&self) -> Option<f64> {
        self.levels[0]
            .keys()
            .map(|&b| f64::from_bits(b))
            .fold(None, |m, v| Some(m.map_or(v, |x: f64| x.max(v))))
    }

    fn best_ask(&self) -> Option<f64> {
        self.levels[1]
            .keys()
            .map(|&b| f64::from_bits(b))
            .fold(None, |m, v| Some(m.map_or(v, |x: f64| x.min(v))))
    }

    fn clean_touch(&self) -> (Option<f64>, Option<f64>) {
        if self.levels[0].is_empty() || self.levels[1].is_empty() {
            return (self.best_bid(), self.best_ask());
        }
        let ask = self.best_ask().unwrap();
        let mut bids: Vec<f64> = self.levels[0].keys().map(|&b| f64::from_bits(b)).collect();
        bids.sort_by(|a, b| b.partial_cmp(a).unwrap()); // descending
        let bid = bids.into_iter().find(|&p| p < ask);
        (bid, Some(ask))
    }

    fn size_at(&self, side: i64, price: f64) -> f64 {
        self.levels[side as usize]
            .get(&key(price))
            .map_or(0.0, |ids| ids.iter().map(|i| self.orders[i].1).sum())
    }

    fn order_ids_at(&self, side: i64, price: f64) -> Vec<i64> {
        self.levels[side as usize].get(&key(price)).cloned().unwrap_or_default()
    }
}

// --- Python-facing L3Book ------------------------------------------------
#[pyclass(name = "L3Book")]
struct PyL3Book {
    inner: Book,
}

#[pymethods]
impl PyL3Book {
    #[new]
    fn new() -> Self {
        PyL3Book { inner: Book::new() }
    }

    fn apply(&mut self, ev: &Bound<'_, PyDict>) -> PyResult<()> {
        let e = parse_one(ev)?;
        self.inner.apply_ev(&e);
        Ok(())
    }

    fn best_bid(&self) -> Option<f64> {
        self.inner.best_bid()
    }

    fn best_ask(&self) -> Option<f64> {
        self.inner.best_ask()
    }

    fn clean_touch(&self) -> (Option<f64>, Option<f64>) {
        self.inner.clean_touch()
    }

    fn size_at(&self, side: i64, price: f64) -> f64 {
        self.inner.size_at(side, price)
    }

    fn order_ids_at(&self, side: i64, price: f64) -> Vec<i64> {
        self.inner.order_ids_at(side, price)
    }
}

// --- PassiveResult -------------------------------------------------------
#[pyclass]
struct PassiveResult {
    #[pyo3(get)]
    side: i64,
    #[pyo3(get)]
    price: f64,
    #[pyo3(get)]
    size: f64,
    #[pyo3(get)]
    enter_ts: i64,
    #[pyo3(get)]
    initial_queue_ahead: f64,
    #[pyo3(get)]
    filled: bool,
    #[pyo3(get)]
    cancelled: bool,
    #[pyo3(get)]
    fill_ts: Option<i64>,
    #[pyo3(get)]
    reached_front_ts: Option<i64>,
    #[pyo3(get)]
    adverse: Option<bool>,
    #[pyo3(get)]
    queue_trace: Vec<(i64, f64)>,
}

#[pymethods]
impl PassiveResult {
    #[getter]
    fn time_to_fill_s(&self) -> Option<f64> {
        self.fill_ts.map(|f| (f - self.enter_ts) as f64 / 1e6)
    }
}

// --- the simulator -------------------------------------------------------
#[pyfunction]
#[pyo3(signature = (events, side, price, enter_idx, size, *, cancel_after_s=None, latency_us=0))]
fn simulate_passive_order(
    events: &Bound<'_, PyList>,
    side: i64,
    price: f64,
    enter_idx: usize,
    size: f64,
    cancel_after_s: Option<f64>,
    latency_us: i64,
) -> PyResult<PassiveResult> {
    let evs = parse_events(events)?;

    let mut book = Book::new();
    for ev in &evs[..enter_idx] {
        if !ev.is_trade {
            book.apply_ev(ev);
        }
    }

    let mut ahead: HashSet<i64> = book.order_ids_at(side, price).into_iter().collect();
    let mut queue_ahead = book.size_at(side, price);
    let enter_ts = evs[enter_idx].t;
    let taker_opp = 1 - side;

    let mut res = PassiveResult {
        side,
        price,
        size,
        enter_ts,
        initial_queue_ahead: queue_ahead,
        filled: false,
        cancelled: false,
        fill_ts: None,
        reached_front_ts: None,
        adverse: None,
        queue_trace: Vec::new(),
    };

    let mut cancel_decided_ts: Option<i64> = None;
    let mut filled_size = 0.0f64;

    for ev in &evs[enter_idx..] {
        let ts = ev.t;
        if let Some(ca) = cancel_after_s {
            if cancel_decided_ts.is_none() && (ts - enter_ts) as f64 / 1e6 >= ca {
                cancel_decided_ts = Some(ts);
            }
        }
        if let Some(cdt) = cancel_decided_ts {
            if ts >= cdt + latency_us && !res.filled {
                res.cancelled = true;
                res.queue_trace.push((ts, queue_ahead.max(0.0)));
                break;
            }
        }

        if !ev.is_trade {
            if ev.e == 3 && ahead.contains(&ev.id) {
                if let Some(o) = book.orders.get(&ev.id) {
                    queue_ahead -= o.1;
                }
                ahead.remove(&ev.id);
            }
            book.apply_ev(ev);
        } else if ev.s == taker_opp && (ev.p - price).abs() < 1e-9 {
            if queue_ahead > 1e-12 {
                queue_ahead -= ev.a;
            } else {
                if res.reached_front_ts.is_none() {
                    res.reached_front_ts = Some(ts);
                }
                filled_size += ev.a;
                if filled_size >= size - 1e-12 {
                    res.filled = true;
                    res.fill_ts = Some(ts);
                    if side == 0 {
                        if let Some(bb) = book.best_bid() {
                            res.adverse = Some(bb < price);
                        }
                    } else if side == 1 {
                        if let Some(ba) = book.best_ask() {
                            res.adverse = Some(ba > price);
                        }
                    }
                    res.queue_trace.push((ts, 0.0));
                    break;
                }
            }
        }
        res.queue_trace.push((ts, queue_ahead.max(0.0)));
    }

    Ok(res)
}

#[pymodule]
fn convexpi_arena_rs(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyL3Book>()?;
    m.add_class::<PassiveResult>()?;
    m.add_function(wrap_pyfunction!(simulate_passive_order, m)?)?;
    Ok(())
}
