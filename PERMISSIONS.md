# PERMISSIONS

Define what the agent can do automatically and what requires approval.

## Default Policy

### Automatic

- Research
- Analysis
- Drafting
- Creating temporary files
- Generating thumbnail concepts
- Image editing
- Running tests
- Preparing reports
- Reading authorized project files

### Approval Required

- Publishing content
- Sending external messages
- Sending emails
- Pushing production deployments
- Making public repository changes
- Uploading public content

### Explicit Confirmation Required

- Spending money
- Deleting important files
- Irreversible destructive operations
- Permanent account changes
- High-impact external actions

## Tool Permissions

```yaml
github:
  read: true
  branch_create: true
  commit: true
  push: approval_required

youtube:
  analytics: true
  draft: true
  upload: approval_required
  publish: approval_required

files:
  read: true
  create: true
  modify: true
  delete: approval_required

email:
  read: approval_required
  draft: true
  send: approval_required
```

Update this file as the agent gains trusted capabilities.
