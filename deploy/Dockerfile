FROM python:3.12-slim

WORKDIR /app

# Install the package first (layer-cached unless these change).
# README.md is required because pyproject.toml declares `readme = "README.md"` (hatchling reads it).
COPY pyproject.toml README.md ./
COPY src/ src/
RUN pip install --no-cache-dir -e ".[dev]"

# Ship the sample order book so book-replay mode (ARENA_CRYPTO_BOOK) works out of the box.
COPY data/ data/

# Default: run the arena server. Use `python -m` (not the console script) so it works regardless
# of whether the scripts bin dir is on PATH.
EXPOSE 8765
CMD ["python", "-m", "convexpi.arena.server"]
