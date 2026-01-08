#!/bin/bash

# Build Lambda deployment package for Spark Workload Analysis
# This script creates a ZIP file with all dependencies for Lambda deployment

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

# Clean up build directory (optional)
read -p "🗑️  Clean up build directory? (y/N): " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    rm -rf ${BUILD_DIR}
    echo "✅ Build directory cleaned up"
fi

echo "🎉 Done! Your Lambda deployment package is ready."
