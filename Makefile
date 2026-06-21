STACK_NAME   := dms
REGION       := $(shell aws configure get region)
SECRET       := $(shell openssl rand -hex 16)

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
		--resolve-image-repos \
		--parameter-overrides EndpointSecret=$(SECRET)
	@echo ""
	@echo "Endpoints:"
	@aws cloudformation describe-stacks \
		--stack-name $(STACK_NAME) \
		--region $(REGION) \
		--query "Stacks[0].Outputs" \
		--output table

# Re-deploy after code changes (reuses the existing secret from the deployed stack).
update:
	$(eval EXISTING_SECRET := $(shell aws ssm get-parameter \
		--name /dms/endpoint-secret \
		--with-decryption \
		--query Parameter.Value \
		--output text 2>/dev/null || echo ""))
	sam build
	sam deploy \
		--stack-name $(STACK_NAME) \
		--region $(REGION) \
		--capabilities CAPABILITY_IAM \
		--resolve-image-repos \
		--parameter-overrides EndpointSecret=$(EXISTING_SECRET)

destroy:
	aws cloudformation delete-stack --stack-name $(STACK_NAME) --region $(REGION)
