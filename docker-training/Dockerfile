FROM pytorch/pytorch:2.0.1-cuda11.8-cudnn8-runtime

WORKDIR /app

# Install Python dependencies
COPY requirements-train.txt .
RUN pip install --no-cache-dir -r requirements-train.txt

# Copy training code and data
COPY scripts/ scripts/
COPY config/ config/
COPY data/ data/

# Create models directory
RUN mkdir -p models

# Set environment variables
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Default command
CMD ["python", "scripts/training/train_lstm.py", "--data-source", "csv", "--csv-path", "data/kospi_mini_1m.csv", "--output", "models/trading_lstm.pth", "--epochs", "100"]
