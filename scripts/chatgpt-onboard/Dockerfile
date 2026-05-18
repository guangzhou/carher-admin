FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

RUN pip install --no-cache-dir \
      pyyaml==6.0.2 \
      requests==2.32.3

WORKDIR /work
COPY oauth-bind.py /work/oauth-bind.py

ENTRYPOINT ["python3", "/work/oauth-bind.py"]
