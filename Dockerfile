FROM python:3.6-alpine
COPY src/python3/requirements.txt /requirements.txt
RUN apk add --no-cache libressl-dev musl-dev libffi-dev gcc
RUN pip3 install -r /requirements.txt

COPY src/python3/bookmate_downloader.py /bin/bookmate_downloader.py

WORKDIR /data
ENTRYPOINT [ "python3", "/bin/bookmate_downloader.py" ]