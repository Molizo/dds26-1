FROM python:3.12-slim

WORKDIR /home/flask-app

COPY order/requirements.txt ./requirements.txt

RUN pip install -r requirements.txt

COPY shared_messaging ./shared_messaging
COPY order/ ./

EXPOSE 5000
