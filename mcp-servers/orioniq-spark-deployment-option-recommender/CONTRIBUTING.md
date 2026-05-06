# Contributing to OrionIQ

Thank you for your interest in contributing to OrionIQ! This document provides guidelines for contributing to the project.

## 🚀 Getting Started

### Prerequisites

- Python 3.8+
- `uv` package manager
- AWS CLI configured
- Git

### Development Setup

1. **Fork and Clone**
   ```bash
   git clone https://github.com/your-username/orioniq-emr-advisor.git
   cd orioniq-emr-advisor
   ```

2. **Setup Development Environment**
   ```bash
   uv sync --dev
   ```

3. **Configure Pre-commit Hooks**
   ```bash
   uv run pre-commit install
   ```

## 🛠️ Development Workflow

### Code Style

We use the following tools for code quality:

```bash
# Format code
uv run black src/
uv run isort src/

# Lint code
uv run flake8 src/
uv run mypy src/

# Run tests
uv run pytest
```

### Commit Messages

Follow conventional commit format:
- `feat:` New features
- `fix:` Bug fixes
- `docs:` Documentation changes
- `refactor:` Code refactoring
- `test:` Test additions/changes

Example:
```
feat: add EMR Serverless cost estimation

- Implement custom cost calculator for EMR Serverless
- Add support for vCPU and memory hour calculations
- Include spot instance pricing considerations
```

## 🧪 Testing

### Running Tests

```bash
# Run all tests
uv run pytest

# Run specific test file
uv run pytest tests/test_cost_estimator.py

# Run with coverage
uv run pytest --cov=src
```

### Writing Tests

- Place tests in the `tests/` directory
- Use descriptive test names
- Include both unit and integration tests
- Mock external dependencies (AWS APIs, MCP servers)

Example test structure:
```python
def test_emr_cost_calculation():
    """Test EMR Serverless cost calculation with sample data."""
    # Arrange
    resource_data = {...}
    
    # Act
    result = calculate_serverless_cost(resource_data)
    
    # Assert
    assert result['total_estimated_cost'] > 0
    assert 'vcpu_hours' in result
```

## 📚 Documentation

### Documentation Standards

- Update README.md for user-facing changes
- Add docstrings to all functions and classes
- Include type hints
- Provide usage examples

### API Documentation

Use Google-style docstrings:

```python
def analyze_cluster_patterns(cluster_id: str, region: str) -> Dict[str, Any]:
    """Analyze EMR cluster usage patterns.
    
    Args:
        cluster_id: EMR cluster identifier (e.g., 'j-XXXXXXXXXXXXX')
        region: AWS region (e.g., 'us-west-2')
        
    Returns:
        Dictionary containing cluster analysis results including:
        - utilization_metrics: Resource utilization data
        - cost_analysis: Cost breakdown and optimization opportunities
        - recommendations: Deployment option recommendations
        
    Raises:
        ValueError: If cluster_id is invalid
        AWSError: If AWS API calls fail
    """
```

## 🐛 Reporting Issues

### Bug Reports

Include the following information:
- OrionIQ version
- Python version
- Operating system
- Steps to reproduce
- Expected vs actual behavior
- Error messages/logs

### Feature Requests

Provide:
- Clear description of the feature
- Use case and business value
- Proposed implementation approach
- Any relevant examples or mockups

## 🔄 Pull Request Process

1. **Create Feature Branch**
   ```bash
   git checkout -b feature/your-feature-name
   ```

2. **Make Changes**
   - Follow code style guidelines
   - Add tests for new functionality
   - Update documentation

3. **Test Your Changes**
   ```bash
   uv run pytest
   uv run black --check src/
   uv run flake8 src/
   ```

4. **Submit Pull Request**
   - Use descriptive title and description
   - Reference related issues
   - Include screenshots for UI changes

### PR Review Criteria

- [ ] Code follows style guidelines
- [ ] Tests pass and coverage is maintained
- [ ] Documentation is updated
- [ ] No breaking changes (or properly documented)
- [ ] Performance impact is considered

## 🏗️ Architecture Guidelines

### MCP Server Development

When adding new MCP servers:
- Follow the existing pattern in `combined_emr_advisor_mcp.py`
- Use FastMCP for server implementation
- Include proper error handling and logging
- Add comprehensive tool descriptions

### Configuration Management

- Use `config.yaml` for user-configurable settings
- Provide sensible defaults
- Validate configuration on startup
- Document all configuration options

## 📦 Release Process

1. Update version in `pyproject.toml`
2. Update CHANGELOG.md
3. Create release PR
4. Tag release after merge
5. Update documentation

## 🤝 Community Guidelines

- Be respectful and inclusive
- Help others learn and grow
- Share knowledge and best practices
- Provide constructive feedback

## 📞 Getting Help

- GitHub Issues for bugs and features
- Discussions for questions and ideas
- Email [maintainer-email] for security issues

Thank you for contributing to OrionIQ! 🚀