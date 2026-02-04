# Use an official Python runtime as a parent image
FROM python:3.12-bookworm

RUN pip install --no-cache-dir --upgrade pip setuptools

# Set the working directory to /app
WORKDIR /app

# Copy the current directory contents into the container at /app
COPY . /app

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Install the AWS CLI to interact with S3
RUN apt-get update && apt-get install -y awscli vim

RUN mkdir chromadb

# Make port 8080 available to the world outside this container
EXPOSE 8080
EXPOSE 8081

# Copy the script into the container
COPY entrypoint.sh /app/entrypoint.sh

# Give execute permissions to the script
RUN chmod +x /app/entrypoint.sh

# Set the entrypoint
ENTRYPOINT ["/app/entrypoint.sh"]
