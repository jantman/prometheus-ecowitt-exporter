FROM python:3.12-alpine

COPY requirements.txt /requirements.txt
RUN pip install -r /requirements.txt
COPY main.py /main.py

EXPOSE 8000

ENTRYPOINT [ "/bin/sh", "-c", "/main.py ${@}", "--" ]
