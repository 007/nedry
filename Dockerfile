FROM python:3.7-slim AS builder
LABEL maintainer "ryan@espressive.com"

ADD Pipfile Pipfile.lock /
RUN pip install pipenv && pipenv lock -r > /requirements.txt

FROM python:3.7-slim
RUN mkdir /nedry
WORKDIR /nedry
COPY --from=builder /requirements.txt /nedry/
COPY nedry.py kube.py /nedry/
RUN pip install -r requirements.txt
CMD ["/nedry.py"]
