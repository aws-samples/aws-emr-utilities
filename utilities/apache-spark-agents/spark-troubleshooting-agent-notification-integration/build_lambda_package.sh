#!/bin/bash

# Build Lambda deployment package for Spark Workload Analysis
# This script creates a ZIP file with all dependencies for Lambda deployment

set -e

echo "🚀 Building Lambda deployment package..."

# Configuration
PACKAGE_NAME="spark-analysis-lambda"
BUILD_DIR="lambda_build"
ZIP_FILE="${PACKAGE_NAME}.zip"

# Clean up previous builds
echo "🧹 Cleaning up previous builds..."
rm -rf ${BUILD_DIR}
rm -f ${ZIP_FILE}

# Create build directory
echo "📁 Creating build directory..."
mkdir -p ${BUILD_DIR}


# Install dependencies
echo "📦 Installing Python dependencies..."
pip install -r requirements.txt --platform manylinux2014_x86_64 -t ${BUILD_DIR} --implementation cp --python-version 3.13 --only-binary=:all:

# pip install -r requirements.txt -t ${BUILD_DIR} --platform manylinux2014_x86_64 --only-binary=:all:

# Copy Lambda function code
echo "📋 Copying Lambda function code..."
cp lambda_function.py ${BUILD_DIR}/

# Create ZIP package
echo "📦 Creating ZIP package..."
cd ${BUILD_DIR}
zip -r ../${ZIP_FILE} . -q
cd ..

# Get package size
PACKAGE_SIZE=$(du -h ${ZIP_FILE} | cut -f1)

echo "✅ Lambda package built successfully!"
echo "📦 Package: ${ZIP_FILE}"
echo "📏 Size: ${PACKAGE_SIZE}"
echo ""
echo "🚀 Next steps:"
echo "1. Upload ${ZIP_FILE} to your Lambda function"
echo "2. Set the handler to: lambda_function.lambda_handler"
echo "3. Set the following environment variables in Lambda:"
echo "   - SNS_TOPIC_ARN: Your SNS topic ARN"
echo "   - STAGE: beta"
echo "   - AWS_REGION: us-east-2"
echo "4. Configure the Lambda execution role with permissions for:"
echo "   - SNS publish permissions"
echo "   - CloudWatch Logs permissions"
echo "5. Set timeout to 300 seconds (5 minutes)"
echo "6. Set memory to 512 MB or higher"
echo "7. Test with the example EventBridge event"
echo ""

# Clean up build directory (optional)
read -p "🗑️  Clean up build directory? (y/N): " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    rm -rf ${BUILD_DIR}
    echo "✅ Build directory cleaned up"
fi

echo "🎉 Done! Your Lambda deployment package is ready."
