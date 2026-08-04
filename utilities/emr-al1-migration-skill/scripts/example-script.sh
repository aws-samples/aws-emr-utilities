#!/bin/bash

# Example Script Template
# This is a simple hello world script demonstrating basic shell scripting patterns

set -e  # Exit on any error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Function to print colored output
print_status() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Main script logic
main() {
    print_status "Starting example script..."
    
    # Example: Check if a command exists
    if command -v echo >/dev/null 2>&1; then
        print_status "Echo command is available"
    else
        print_error "Echo command not found"
        exit 1
    fi
    
    # Example: Simple operation
    print_status "Executing hello world operation..."
    echo "Hello, World!"
    
    # Example: Create a simple file
    TEMP_FILE="/tmp/example-skill-test.txt"
    echo "This is a test file created by the example skill" > "$TEMP_FILE"
    print_status "Created test file: $TEMP_FILE"
    
    # Example: Clean up
    rm -f "$TEMP_FILE"
    print_status "Cleaned up test file"
    
    print_status "Example script completed successfully!"
}

# Error handling
trap 'print_error "Script failed on line $LINENO"' ERR

# Run main function
main "$@"
