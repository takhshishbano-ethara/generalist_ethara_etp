# Single-stage: Playwright image (Ubuntu Jammy, glibc 2.35) + Lambda RIC.
# Using the Playwright image directly avoids glibc mismatch that occurs when
# copying Chromium binaries into Amazon Linux 2 (glibc 2.26).
FROM mcr.microsoft.com/playwright/python:v1.59.0-jammy

RUN pip install --no-cache-dir awslambdaric

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

ENV LAMBDA_TASK_ROOT=/var/task
WORKDIR ${LAMBDA_TASK_ROOT}

COPY config.py ${LAMBDA_TASK_ROOT}/
COPY handler.py ${LAMBDA_TASK_ROOT}/
COPY modules/ ${LAMBDA_TASK_ROOT}/modules/
COPY scripts/ ${LAMBDA_TASK_ROOT}/scripts/
COPY templates/ ${LAMBDA_TASK_ROOT}/templates/

ENTRYPOINT ["python", "-m", "awslambdaric"]
CMD ["handler.lambda_handler"]
