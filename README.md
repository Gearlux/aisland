# Development Sandbox

This directory contains the infrastructure and server management tools for the development environment. It is designed to be cross-platform and easily portable across development machines.

## Prerequisites
- **Python 3.12+**
- **Java OpenJDK 17 or higher** (Required for Jenkins)

## Setup & Usage

### 1. Check Status
Retrieve service information for all infrastructure components:
```bash
sandbox status
```

### 2. Manage Jenkins
```bash
# Start Jenkins (foreground)
sandbox jenkins start

# Start Jenkins (background)
sandbox jenkins start --background

# Stop/Status
sandbox jenkins stop
sandbox jenkins status
```
Default: [http://localhost:8080](http://localhost:8080)

### 3. Manage MLflow
```bash
# Start MLflow (foreground)
sandbox mlflow start

# Start MLflow (background)
sandbox mlflow start --background

# Stop/Status
sandbox mlflow stop
sandbox mlflow status
```
Default: [http://localhost:5000](http://localhost:5000)

## Jenkins Configuration
- **JENKINS_HOME:** All data, jobs, and configurations are stored in `sandbox/data/jenkins`. This directory is ignored by Git to keep your local environment clean.
- **Pipelines:** Each project (e.g., `logflow/`) contains its own `Jenkinsfile`. When creating a new job in Jenkins, point it to the local repository path and select "Pipeline from SCM" with the script path set to `logflow/Jenkinsfile`.

## Future Services
Placeholders for **LabelStudio** and **Grafana/Loki** are being prepared and will be integrated into the `sandbox` CLI in future updates.
