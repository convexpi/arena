FROM python:3.12-slim

WORKDIR /app

# Install the package first (layer-cached unless pyproject.toml changes)
COPY pyproject.toml .
COPY src/ src/
RUN pip install --no-cache-dir -e ".[dev]"

# Default: run the arena server
EXPOSE 8765
CMD ["convexpi-server"]
