FROM python:3.14-slim

WORKDIR /home/flask-app

COPY orchestrator/requirements.txt .
RUN pip install -r requirements.txt

COPY common/ ./common/
COPY coordinator/ ./coordinator/
COPY orchestrator/ .

EXPOSE 5000
