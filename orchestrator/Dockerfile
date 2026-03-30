FROM python:3.14-slim

WORKDIR /home/flask-app

COPY orchestrator/requirements.txt ./orchestrator/requirements.txt
RUN pip install -r orchestrator/requirements.txt

COPY common/ ./common/
COPY orchestrator/ ./orchestrator/

EXPOSE 5000
