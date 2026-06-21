STACK_NAME   := dms
REGION       := $(shell aws configure get region)
SECRET       := $(shell openssl rand -hex 16)
EMAIL        := me@chrisrothwell.com

.PHONY: deploy destroy secret

# First-time deploy: generates a secret, builds the container, pushes to ECR, deploys the stack.
# The secret is printed at the end — save it somewhere safe.
deploy:
	@echo "Generated endpoint secret: $(SECRET)"
	sam build
	sam deploy \
		--stack-name $(STACK_NAME) \
		--region $(REGION) \
		--capabilities CAPABILITY_IAM \
		--resolve-s3 \
		--resolve-image-repos \
		--parameter-overrides EndpointSecret=$(SECRET) NotificationEmail=$(EMAIL)
	@echo ""
	@echo "Endpoints:"
	@aws cloudformation describe-stacks \
		--stack-name $(STACK_NAME) \
		--region $(REGION) \
		--query "Stacks[0].Outputs" \
		--output table

# Re-deploy after code changes (reuses the existing secret from the deployed stack).
update:
	sam build
	sam deploy \
		--stack-name $(STACK_NAME) \
		--region $(REGION) \
		--capabilities CAPABILITY_IAM \
		--resolve-s3 \
		--resolve-image-repos \
		--parameter-overrides NotificationEmail=$(EMAIL)

destroy:
	aws cloudformation delete-stack --stack-name $(STACK_NAME) --region $(REGION)
