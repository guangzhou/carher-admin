# Capture image: patchright (real-Chrome TLS) on top of the Playwright base
# that already ships chromium-1223. Used by ops/refresh.sh to re-grab a fresh
# /backend-api/f/conversation request (cf_clearance + sentinel proof token)
# while reusing the persistent browser profile (no OTP unless fully expired).
FROM mcr.microsoft.com/playwright/python:v1.60.0-noble

RUN pip install --no-cache-dir patchright \
    && python -m patchright install chromium

COPY zerokey-web-capture.py /capture/zerokey-web-capture.py

ENV PYTHONUNBUFFERED=1
WORKDIR /work

# headless=False inside the script → needs a virtual display.
# NOTE: run via `bash -lc` — invoking xvfb-run directly as PID 1 hangs before
# it ever exec's python (no TTY / signal-reaping quirk).
# The trailing `; exit $?` keeps bash as PID 1 (a command LIST disables bash's
# exec optimization), so xvfb-run runs as a child — invoking it as PID 1 hangs.
ENTRYPOINT ["bash", "-lc", "xvfb-run -a python /capture/zerokey-web-capture.py; exit $?"]
