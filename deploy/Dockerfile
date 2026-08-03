FROM python:3.12-slim

WORKDIR /app

# Install the package first (layer-cached unless these change).
# README.md is required because pyproject.toml declares `readme = "README.md"` (hatchling reads it).
COPY pyproject.toml README.md ./
COPY src/ src/
# `[server]` — websockets + rich — is what `python -m convexpi.arena.server` actually needs. This
# said `[dev]` (pytest, hatch), which installs no websockets, so every container started and then
# died on `ModuleNotFoundError: No module named 'websockets'`. Nothing runs tests inside the image,
# so the dev tools were never needed here.
# Not `[all]`: the GARCH fundamental's finmlsim import is lazy and unreachable from the server's
# env-var surface, so it would be weight for nothing.
RUN pip install --no-cache-dir -e ".[server]"

# Ship the sample order book so book-replay mode (ARENA_CRYPTO_BOOK) works out of the box.
COPY data/ data/

# Default: run the arena server. Use `python -m` (not the console script) so it works regardless
# of whether the scripts bin dir is on PATH.
EXPOSE 8765
CMD ["python", "-m", "convexpi.arena.server"]
