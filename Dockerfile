FROM python:3.11-slim

# Prevent Python from writing .pyc files and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    PATH="/root/.local/bin:$PATH"

WORKDIR /app

# Install curl and ca-certificates
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install the agy binary
RUN curl -fsSL https://antigravity.google/cli/install.sh | bash

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY proxy.py .

# Expose proxy service port
EXPOSE 8000

# Run FastAPI app with Uvicorn
CMD ["uvicorn", "proxy:app", "--host", "0.0.0.0", "--port", "8000"]
