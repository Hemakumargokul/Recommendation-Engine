from openai import OpenAI
import logging
from flask import Flask, request, jsonify
import os

app = Flask(__name__)

# Initialize the OpenAI client
client = OpenAI(api_key=os.environ.get('GPT_API_KEY'))

# Set up logging
logging.basicConfig(filename='image_analysis.log', level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')


def get_alt_text(image_url, image_title=None):
    # Log the request
    logging.info(f"Generating alt text for image URL: {image_url} with title: {image_title}")

    # Create the chat completion request
    try:
        messages = [
            {
                "role": "system",
                "content": "You are an assistant that generates descriptive alt text for images. "
                           "Follow SEO guidelines for alt text generation. "
                           "Keep your alt text fewer than 100 characters."
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"Generate a descriptive alt text for the following image "
                                             f"{'titled ' + image_title if image_title else ''}. "
                                             f"Use the file name if it is appropriate. "
                                             f"Keep your alt text fewer than 100 characters. "
                                             f"Follow SEO guidelines for alt text generation. "
                                             f"If the image contains car, include the color of the car and view of the car in the alt text. "
                                             f"Here is the image url:"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": image_url,
                        },
                    },
                ],
            }
        ]

        response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            max_tokens=300,
            temperature=0,
            top_p=1,
        )

        alt_text = response.choices[0].message.content
        logging.info(f"Generated alt text: {alt_text}")
        return alt_text
    except Exception as e:
        logging.error(f"Error generating alt text: {e}")
        raise

@app.route('/generate-alt-text', methods=['GET'])
def generate_alt_text():
    image_url = request.args.get('image_url')
    image_title = request.args.get('image_title')
    if not image_url:
        logging.warning("Image URL is required but not provided")
        return jsonify({"error": "Image URL is required"}), 400

    try:
        if image_title and image_title != 'undefined':
            alt_text = get_alt_text(image_url, image_title)
        else:
            alt_text = get_alt_text(image_url)
        logging.info(f"Returning alt text for image URL: {image_url}")
        return jsonify({"image_url": image_url, "alt_text": alt_text})
    except Exception as e:
        logging.error(f"Error in /generate-alt-text endpoint: {e}")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    logging.info("Starting Flask server")
    app.run(host='0.0.0.0', port=8081, debug=True)
