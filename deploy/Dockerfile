FROM python:3.12-slim

WORKDIR /app

# Install the package first (layer-cached unless pyproject.toml changes)
COPY pyproject.toml .
COPY src/ src/
RUN pip install --no-cache-dir -e ".[dev]"

# Ship the sample order book so book-replay mode (ARENA_CRYPTO_BOOK) works out of the box.
COPY data/ data/

# Default: run the arena server
EXPOSE 8765
CMD ["convexpi-server"]
