#!/bin/bash

# Get the value of the environment variable
ROLE=$AWS_ROLE_ARN

# Check if ROLE contains 'production'
if [[ "$ROLE" == *"production"* ]]; then
  ENV='production'
elif [[ "$ROLE" == *"staging"* ]]; then
  ENV='staging'
else
  ENV='dev'
fi


echo "Copying data from S3 to local directory..."
aws s3 cp --recursive s3://autoblog-ai-${ENV}-${AWS_REGION}/chromadb chromadb --sse AES256

if [[ $COPY_RESULT -ne 0 ]]; then
  echo "Failed to copy data from S3"
  exit 1
fi

echo "Data copied successfully"

# Start the first application on port 8080
echo "Starting recommendation-engine/retriever.py on port 8080..."
python recommendation-engine/retriever.py &

# Start the Flask application on port 8081
echo "Starting image-analysis-engine/generator.py on port 8081..."
python image-analysis-engine/generator.py &

# Keep the container running indefinitely
tail -f /dev/null
