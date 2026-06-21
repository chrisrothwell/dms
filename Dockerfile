FROM ghcr.io/ekristen/aws-nuke:v3.64.1 AS aws-nuke-binary

FROM public.ecr.aws/lambda/python:3.12

COPY --from=aws-nuke-binary /usr/local/bin/aws-nuke /usr/local/bin/aws-nuke

COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install -r requirements.txt --no-cache-dir

COPY src/ ${LAMBDA_TASK_ROOT}/

CMD ["app.lambda_handler"]
