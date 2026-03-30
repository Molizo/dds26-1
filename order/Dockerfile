FROM python:3.14-slim

WORKDIR /home/flask-app

COPY order/requirements.txt .
RUN pip install -r requirements.txt

COPY common/ ./common/
COPY order/ .

EXPOSE 5000
