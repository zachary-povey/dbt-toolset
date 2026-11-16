# Reference image for the Lambda behind an AgentCore Gateway target.
# Compile your dbt project first, then build with the artifact in the context:
#
#   dbt-toolset compile --project-dir my_dbt_project --out toolset/
#   docker build --build-arg ARTIFACT=toolset/toolset.json -t my-toolset .
FROM public.ecr.aws/lambda/python:3.13

COPY pyproject.toml README.md ${LAMBDA_TASK_ROOT}/build/
COPY src ${LAMBDA_TASK_ROOT}/build/src
RUN pip install --no-cache-dir "${LAMBDA_TASK_ROOT}/build[runtime]" && rm -rf "${LAMBDA_TASK_ROOT}/build"

ARG ARTIFACT=toolset/toolset.json
COPY ${ARTIFACT} ${LAMBDA_TASK_ROOT}/toolset.json
ENV DBT_TOOLSET_ARTIFACT=${LAMBDA_TASK_ROOT}/toolset.json

CMD ["dbt_toolset.runtime.lambda_handler.handler"]
