FROM python:3.12-slim

WORKDIR /home/flask-app

# Install dependencies first (layer cache)
COPY stock/requirements.txt .
RUN pip install -r requirements.txt

# Copy shared packages before service code
COPY common/ ./common/
COPY stock/ .

EXPOSE 5000
