FROM mcr.microsoft.com/playwright/python:v1.59.0-jammy

RUN pip install --no-cache-dir awslambdaric

COPY --from=lambda requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

ENV LAMBDA_TASK_ROOT=/var/task
WORKDIR ${LAMBDA_TASK_ROOT}

COPY --from=lambda config.py ${LAMBDA_TASK_ROOT}/
COPY --from=lambda handler.py ${LAMBDA_TASK_ROOT}/
COPY --from=lambda modules/ ${LAMBDA_TASK_ROOT}/modules/
COPY --from=lambda scripts/ ${LAMBDA_TASK_ROOT}/scripts/
COPY --from=lambda templates/ ${LAMBDA_TASK_ROOT}/templates/

ADD https://github.com/aws/aws-lambda-runtime-interface-emulator/releases/latest/download/aws-lambda-rie /usr/local/bin/aws-lambda-rie
RUN chmod +x /usr/local/bin/aws-lambda-rie

ENTRYPOINT ["/usr/local/bin/aws-lambda-rie", "python", "-m", "awslambdaric"]
CMD ["handler.lambda_handler"]
