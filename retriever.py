import os
import shutil
import logging
import threading
from contextlib import closing
from datetime import timedelta, datetime
import re
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request
from mysql.connector import connect, Error
import chromadb
from chromadb.utils import embedding_functions
from langchain.text_splitter import RecursiveCharacterTextSplitter
import boto3
from botocore.exceptions import NoCredentialsError
from ouroath.yamas.collector.api import YamasCollectorAPI
from ouroath.yamas.collector.endpoints import PUBLIC
from ouroath.yamas.collector.exceptions import YamasError
from ouroath.yamas.collector.message import YamasMessage
from apscheduler.schedulers.background import BackgroundScheduler
import time

logging.basicConfig(filename='api_results.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Initialize Flask app
app = Flask(__name__)

# Initialize variables for metric aggregation
metrics = {}
timer = None

# Initialize ChromaDB client and text splitter
text_splitter = RecursiveCharacterTextSplitter(chunk_size=8000, chunk_overlap=0)
client = chromadb.PersistentClient(path="/app/chromadb")
openai_ef = embedding_functions.OpenAIEmbeddingFunction(api_key=os.environ.get('GPT_API_KEY'),
                                                        model_name="text-embedding-3-small")
collection = client.get_or_create_collection(name="embedded_posts", embedding_function=openai_ef)
ROLE = os.environ.get('AWS_ROLE_ARN')
if ROLE and 'production' in ROLE:
    ENV = 'production'
elif ROLE and 'staging' in ROLE:
    ENV = 'staging'
else:
    ENV = 'dev'

# Preprocess text content
def preprocess_text(text):
    # Remove HTML tags
    soup = BeautifulSoup(text, 'html.parser')
    text = soup.get_text()

    # Lowercasing
    text = text.lower()

    # Remove URLs
    text = re.sub(r'http\S+', '', text)

    # Remove special characters and punctuation
    text = re.sub(r'[^a-zA-Z0-9\s$]', '', text)

    # Remove extra whitespace
    text = re.sub(r'\s+', ' ', text).strip()

    return text


# Function to fetch results from ChromaDB
def get_results(post_content_id, nresults):
    try:
        logging.info(f"Fetching results for post_content_id: {post_content_id}...")
        # Query the database using the provided post_content_id
        # Modify the SQL query based on your table structure
        sql_query = f"SELECT post_content FROM wp_posts WHERE id = {post_content_id}"
        one_year_ago = datetime.now() - timedelta(days=365)
        timestamp_one_year_ago = int(one_year_ago.timestamp())
        with closing(connect(host=os.environ.get('AUTOBLOG_BLOG_DB'), database='blog', user='wp_rw',
                             password=os.environ.get('AUTOBLOG_BLOG_RW_PASSWORD'), port='3306')) as connection:
            with closing(connection.cursor()) as cursor:
                cursor.execute(sql_query)
                post_content = (cursor.fetchone()[0])
                # Preprocess text content
                preprocessed_content = preprocess_text(post_content)
                logging.info(f"Count: {collection.count()}")
                # Split text into chunks for embedding
                tokenized_post_content_query = text_splitter.create_documents([preprocessed_content])[0].page_content

                result = collection.query(query_texts=tokenized_post_content_query, n_results=nresults,
                                          include=["documents", 'distances', 'metadatas', ])
                if result:
                    logging.info(f"Results fetched successfully: {result['ids']}")
                    # Filter out post_content_id from the results
                    filtered_ids = [id for id in result['ids'][0] if id != str(post_content_id)]
                    if filtered_ids:
                        return filtered_ids
                    else:
                        increment_metric('api_results_empty')
                        logging.info("No valid results found after filtering out the current post_content_id.")
                        return None

    except Error as e:
        increment_metric('api_errors')
        logging.error(f"Error in database connection: {e}")
    except Exception as e:
        increment_metric('api_exception')
        logging.error(f"Exception while processing get_results: {e}")

    return None


# Route to handle API requests
@app.route('/api/results', methods=['GET'])
def api_results():
    try:
        post_content_id = request.args.get('post_content_id', type=int)
        nresults = request.args.get('nresults', default=6, type=int)
        result = get_results(post_content_id, nresults)

        # Aggregate metric for API requests
        increment_metric('api_requests')

        # Check if results are not null or empty
        if not result:
            increment_metric('api_results_empty')

        return jsonify(result)
    except Error as e:
        logging.error(f"Error in api_results: {e}")
        increment_metric('api_errors')
    except Exception as e:
        logging.error(f"Exception in api_results: {e}")
        increment_metric('api_exception')

    return jsonify({'error': 'Internal Server Error'}), 500


# Function to send aggregated metrics
def send_aggregated_metrics():
    try:
        if metrics:
            logging.info("Sending send_aggregated_metrics")
            # Send aggregated metrics
            yamas_api = YamasCollectorAPI(
                namespace=os.environ.get('YAMAS_NAMESPACE'),
                endpoint=PUBLIC,
                key_path=os.environ.get('SIA_KEY_PATH'),
                cert_path=os.environ.get('SIA_CERT_PATH')
            )
            message = YamasMessage(application='autoblog', metrics=metrics)
            yamas_api.send_message(message)
            logging.info("Aggregated metrics sent successfully")
            metrics.clear()  # Reset metrics dictionary after sending
    except YamasError as error:
        logging.error(f"Error sending aggregated metrics: {error}")
        timer.cancel()


# Start timer to send aggregated metrics every second
def start_timer():
    threading.Timer(60.0, start_timer).start()
    send_aggregated_metrics()


# Function to send aggregated metrics once
def send_aggregated_metrics_once():
    global timer
    if timer is None or not timer.is_alive():
        send_aggregated_metrics()
        timer = threading.Timer(60.0, start_timer)
        timer.start()


# Function to increment metrics
def increment_metric(metric_name):
    if metric_name in metrics:
        metrics[metric_name] += 1
    else:
        metrics[metric_name] = 1


# Function to update ChromaDB collection
def update_chroma_collection():
    try:
        logging.info("Updating ChromaDB collection...")
        global client, collection
        text_splitter = RecursiveCharacterTextSplitter(chunk_size=8000, chunk_overlap=0)
        # Fetch data from the database
        documents = []
        metadatas = []
        ids = []
        try:
            with closing(connect(host=os.environ.get('AUTOBLOG_BLOG_DB'), database='blog', user='wp_rw',
                                 password=os.environ.get('AUTOBLOG_BLOG_RW_PASSWORD'),
                                 port='3306')) as connection:
                with closing(connection.cursor()) as cursor:
                    delete_s3_folder_contents()
                    sql_query = """
                            SELECT DISTINCT combined.ID, combined.post_content, combined.post_date
                            FROM (
                                -- Query 1: Get posts with the tag "_evergreen" modified in the last 6 months
                                SELECT wp1.ID, wp1.post_content, wp1.post_date
                                FROM wp_posts wp1
                                WHERE wp1.ID IN (
                                    SELECT object_id 
                                    FROM wp_term_relationships 
                                    WHERE term_taxonomy_id IN (
                                        SELECT term_taxonomy_id 
                                        FROM wp_term_taxonomy 
                                        WHERE term_id IN (
                                            SELECT term_id 
                                            FROM wp_terms 
                                            WHERE name = '_evergreen'
                                        ) 
                                        AND taxonomy = 'post_tag'
                                    )
                                )
                                AND wp1.post_status = 'publish'
                                AND wp1.post_type = 'post'
                                AND wp1.post_modified > DATE_SUB(NOW(), INTERVAL 6 MONTH)
                                AND wp1.post_author NOT IN (6301, 5873, 5947, 6430, 6524, 6554, 5684)

                                UNION ALL

                                -- Query 2: Get posts with the category "commerce" modified in the last 1 month
                                SELECT wp2.ID, wp2.post_content, wp2.post_date
                                FROM wp_posts wp2
                                WHERE wp2.ID IN (
                                    SELECT object_id 
                                    FROM wp_term_relationships 
                                    WHERE term_taxonomy_id IN (
                                        SELECT term_taxonomy_id 
                                        FROM wp_term_taxonomy 
                                        WHERE term_id IN (
                                            SELECT term_id 
                                            FROM wp_terms 
                                            WHERE name = 'commerce'
                                        ) 
                                        AND taxonomy = 'category'
                                    )
                                )
                                AND wp2.post_status = 'publish'
                                AND wp2.post_type = 'post'
                                AND wp2.post_modified > DATE_SUB(NOW(), INTERVAL 1 MONTH)
                                AND wp2.post_author NOT IN (6301, 5873, 5947, 6430, 6524, 6554, 5684)

                                UNION ALL

                                -- Query 3: Get distinct popular posts excluding specific authors
                                SELECT wpp.ID, wpp.post_content, wpp.post_date
                                FROM wp_posts wpp
                                JOIN popular_posts wppp ON wpp.ID = wppp.post_id
                                WHERE wpp.post_author NOT IN (6301, 5873, 5947, 6430, 6524, 6554, 5684) 
                                AND wpp.post_modified > DATE_SUB(NOW(), INTERVAL 1 YEAR)
                                
                                UNION ALL

                                -- Query 4: Get latest published articles from wp_posts within one week from the current date
                                SELECT wp3.ID, wp3.post_content, wp3.post_date
                                FROM wp_posts wp3
                                WHERE wp3.post_status = 'publish'
                                AND wp3.post_type = 'post'
                                AND wp3.post_date > DATE_SUB(NOW(), INTERVAL 1 WEEK)
                                AND wp3.post_author NOT IN (6301, 5873, 5947, 6430, 6524, 6554, 5684)
                            ) AS combined
                            ORDER BY combined.post_date;
                        """
                    cursor.execute(sql_query)
                    results = cursor.fetchall()
                    for r in results:
                        post_id, post_content, post_date = r
                        if post_content:
                            preprocessed_content = preprocess_text(post_content)
                            tokenized_post_content = text_splitter.create_documents([preprocessed_content])[
                                0].page_content
                            post_timestamp = int(post_date.timestamp())
                            documents.append(tokenized_post_content)
                            metadatas.append({
                                "formatted_date": post_date.strftime("%Y-%m-%d %H:%M:%S"),
                                "timestamp": post_timestamp
                            })
                            ids.append(str(post_id))

        except Error as e:
            logging.error(f"Error in database connection: {e}")

        finally:
            if connection:
                connection.close()

        if documents:
            client.delete_collection(name="embedded_posts")
            collection = client.get_or_create_collection(name="embedded_posts", embedding_function=openai_ef)
            collection.add(documents=documents, metadatas=metadatas, ids=ids)
            if collection.count():
                logging.info(f"Updated collection count: {collection.count()}")
                s3_upload()
            logging.info(f"ChromaDB collection updated successfully. Total records: {collection.count()}")

        logging.info("ChromaDB collection update completed.")
    except Exception as e:
        logging.error(f"Error in update_chroma_collection: {e}")


def delete_s3_folder_contents():
    logging.info("Running delete s3 contents...")
    s3 = boto3.client('s3')
    aws_region = os.getenv('AWS_REGION', 'us-east-1')  # Default to 'us-east-1' if not set
    bucket_name = f'autoblog-ai-{ENV}-{aws_region}'
    s3_path = 'chromadb/'  # Updated S3 path to 'chromadb/'
    try:
        response = s3.list_objects_v2(Bucket=bucket_name, Prefix=s3_path)
        if 'Contents' in response:
            for obj in response['Contents']:
                s3.delete_object(Bucket=bucket_name, Key=obj['Key'])

        time.sleep(300)
        logging.info(f"All objects in {s3_path} deleted successfully")

    except NoCredentialsError:
        logging.error("Credentials not available")
    except Exception as e:
        logging.error(f"Failed to delete objects in {s3_path}: {e}")

# Function to upload ChromaDB to S3
def upload_to_s3(local_path, bucket_name, s3_path):
    # Create an S3 client
    s3 = boto3.client('s3')
    try:
        # Upload the entire directory to S3
        for root, dirs, files in os.walk(local_path):
            for file in files:
                local_file_path = os.path.join(root, file)
                s3_key = os.path.relpath(local_file_path, local_path)
                s3_object_key = os.path.join(s3_path, s3_key)
                s3.upload_file(local_file_path, bucket_name, s3_object_key,
                               ExtraArgs={'ServerSideEncryption': 'AES256'})

        logging.info("Upload successful")

    except NoCredentialsError:
        logging.error("Credentials not available")

    except Exception as e:
        logging.error(f"Upload failed: {e}")


# Function to upload ChromaDB to S3
def s3_upload():
    logging.info("Running upload_to_s3...")
    aws_region = os.getenv('AWS_REGION', 'us-east-1')  # Default to 'us-east-1' if not set
    s3_bucket_name = f'autoblog-ai-{ENV}-{aws_region}'
    local_chromadb_path = '/app/chromadb/'
    s3_chromadb_path = 'chromadb/'  # Updated S3 path to 'chromadb/'

    upload_to_s3(local_chromadb_path, s3_bucket_name, s3_chromadb_path)


def download_from_s3(bucket_name, s3_path, local_path):
    # Create an S3 client
    s3 = boto3.client('s3')
    try:
        # List objects in the specified S3 path
        response = s3.list_objects_v2(Bucket=bucket_name, Prefix=s3_path)
        if 'Contents' in response:
            for obj in response['Contents']:
                s3_key = obj['Key']
                if s3_key.endswith('/'):
                    continue  # Skip directories
                local_file_path = os.path.join(local_path, os.path.relpath(s3_key, s3_path))
                os.makedirs(os.path.dirname(local_file_path), exist_ok=True)
                s3.download_file(bucket_name, s3_key, local_file_path)

        logging.info("Download successful")

    except NoCredentialsError:
        logging.error("Credentials not available")

    except Exception as e:
        logging.error(f"Download failed: {e}")


def s3_download(local_path):
    print("Running s3_download...")
    aws_region = os.getenv('AWS_REGION', 'us-east-1')  # Default to 'us-east-1' if not set
    s3_bucket_name = f'autoblog-ai-{ENV}-{aws_region}'
    local_chromadb_path = local_path
    s3_chromadb_path = 'chromadb/'

    download_from_s3(s3_bucket_name, s3_chromadb_path, local_chromadb_path)


def sync_chromadb():
    logging.info("Syncing ChromaDB collection...")
    global client
    global collection
    now = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    tmp_dir = f"/tmp/chromadb_{now}"
    s3_download(tmp_dir)
    client_tmp = chromadb.PersistentClient(path=tmp_dir)
    collection_tmp = client_tmp.get_or_create_collection(name="embedded_posts", embedding_function=openai_ef)
    count = collection_tmp.count()

    documents, metadatas, ids, embeddings = [], [], [], []
    for i in range(0, collection_tmp.count(), 10):
        batch = collection_tmp.get(
            include=["metadatas", "documents", "embeddings"],
            limit=10,
            offset=i)
        ids.extend(batch["ids"])
        metadatas.extend(batch["metadatas"])
        documents.extend(batch["documents"])
        embeddings.extend(batch["embeddings"])

    # Add documents to the target collection
    if documents:
        client.delete_collection(name="embedded_posts")
        collection = client.get_or_create_collection(name="embedded_posts", embedding_function=openai_ef)
        collection.add(documents=documents, metadatas=metadatas, ids=ids)
        clear_directory(tmp_dir)
        logging.info(f"Updated collection count: {collection.count()}")
    logging.info(f"ChromaDB collection synced successfully. Total records: {count}")


def clear_directory(dir):
    # Check if the directory exists
    if not os.path.exists(dir):
        return
    # Clear the destination directory
    if os.path.exists(dir):
        shutil.rmtree(dir)


def call_scheduler(hour, minute, function):
    scheduler = BackgroundScheduler()
    scheduler.start()
    now = datetime.now()
    next_run_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if next_run_time < now:
        next_run_time += timedelta(days=1)
    # Schedule the update_chroma_collection function to run daily at 9:00 AM UTC
    scheduler.add_job(function, 'interval', hours=24, start_date=next_run_time)


if ROLE and 'production' in ROLE and 'east' in ROLE:
    call_scheduler(hour=9, minute=00, function=update_chroma_collection)
else:
    call_scheduler(hour=9, minute=30, function=sync_chromadb)

if __name__ == '__main__':
    try:
        logging.info("API Results service started.")

        # Start the timer for metric aggregation
        send_aggregated_metrics_once()

        # Run the Flask app
        app.run(host='0.0.0.0', port=8080)


    except KeyboardInterrupt:
        logging.info("API Results service terminated by user.")
    except Exception as e:
        logging.error(f"Unexpected error in API Results service: {e}")
