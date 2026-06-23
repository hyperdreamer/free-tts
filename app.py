# app.py
from flask import Flask, request, send_file, jsonify
from flask_cors import CORS
import edge_tts
import asyncio
from io import BytesIO
import xml.etree.ElementTree as ET # Import the XML parser

app = Flask(__name__)
CORS(app)

# Define some reasonable defaults for TTS parameters
# These use formats known to be accepted by edge-tts for "no change"
#DEFAULT_VOICE = "en-US-JennyNeural" # A common and reliable default voice
DEFAULT_VOICE = "en-US-AvaMultilingualNeural"
DEFAULT_RATE = "+0%"  # Relative change: no change to rate
DEFAULT_PITCH = "+0Hz" # Relative change: no change to pitch

@app.route('/generate-and-download-tts', methods=['POST'])
async def generate_and_download_tts():
    data = request.json
    ssml_text = data.get('ssml', '').strip()

    if not ssml_text:
        return jsonify({"error": "No SSML text provided"}), 400

    output_filename = "abc.mp3"

    # Initialize extracted parameters with defaults
    extracted_voice = DEFAULT_VOICE
    extracted_rate = DEFAULT_RATE
    extracted_pitch = DEFAULT_PITCH
    extracted_content = "" # This will hold the text to be spoken

    try:
        # 1. Parse the SSML string
        root = ET.fromstring(ssml_text)
        
        # Define the SSML namespace for robust parsing
        ssml_namespace = "http://www.w3.org/2001/10/synthesis"

        # 2. Extract Voice Information
        # Use the namespace to find elements correctly
        voice_element = root.find(f'{{{ssml_namespace}}}voice')
        
        if voice_element is not None:
            voice_name_attribute = voice_element.get('name')
            if voice_name_attribute:
                extracted_voice = voice_name_attribute
            
            # 3. Extract Prosody and Content (assuming prosody is a child of voice)
            prosody_element = voice_element.find(f'{{{ssml_namespace}}}prosody')
            
            if prosody_element is not None:
                # Get raw attribute values from SSML
                rate_attr_raw = prosody_element.get('rate')
                pitch_attr_raw = prosody_element.get('pitch')

                # --- Transformation Logic for Rate ---
                if rate_attr_raw:
                    try:
                        rate_percentage_val = float(rate_attr_raw.strip('%'))
                        if '%' in rate_attr_raw:
                            if rate_percentage_val == 0:
                                extracted_rate = "+0%" # Safely map "0%" to "+0%"
                            elif rate_percentage_val > 0 and rate_percentage_val < 100:
                                # Interpret as a slow-down relative to 100% normal.
                                # e.g., 45% -> -55% (55% slower)
                                extracted_rate = f"-{100 - rate_percentage_val}%"
                            else: # e.g., 150% -> +50% (50% faster)
                                extracted_rate = f"+{rate_percentage_val - 100}%"
                        else: # Not a percentage (e.g., "medium", "+20ms")
                            extracted_rate = rate_attr_raw
                    except ValueError: # If value is like "x-slow", "fast" etc.
                        extracted_rate = rate_attr_raw # Let edge-tts handle named rates
                else:
                    extracted_rate = DEFAULT_RATE # Use default if attribute not found

                # --- Transformation Logic for Pitch ---
                if pitch_attr_raw:
                    try:
                        pitch_percentage_val = float(pitch_attr_raw.strip('%'))
                        if '%' in pitch_attr_raw and pitch_percentage_val == 0:
                            extracted_pitch = "+0Hz" # Safely map "0%" to "+0Hz"
                        else: # Keep as is (e.g., "+20Hz", "x-low")
                            extracted_pitch = pitch_attr_raw
                    except ValueError: # If value is like "x-low", "high" etc.
                        extracted_pitch = pitch_attr_raw # Let edge-tts handle named pitches
                else:
                    extracted_pitch = DEFAULT_PITCH # Use default if attribute not found

                # Extract content from prosody tag
                extracted_content = prosody_element.text.strip() if prosody_element.text else ""
                
            else: # If no <prosody> tag, try to get text directly from <voice> element
                extracted_content = voice_element.text.strip() if voice_element.text else ""
                print(f"Warning: No <prosody> tag found in SSML. Using text directly from <voice> element: '{extracted_content}' and default rate/pitch.")
        else: # If no <voice> tag, try to get text directly from <speak> element
            extracted_content = root.text.strip() if root.text else ""
            print(f"Warning: No <voice> tag found in SSML. Using text directly from <speak> element: '{extracted_content}' and default voice/rate/pitch.")

        if not extracted_content:
            return jsonify({"error": "No speech content found in SSML to synthesize"}), 400

        print(f"DEBUG Extracted & Transformed Parameters: Voice='{extracted_voice}', Rate='{extracted_rate}', Pitch='{extracted_pitch}', Content='{extracted_content}'")

    except ET.ParseError as e:
        print(f"SSML parsing error: {e}. Ensure SSML is well-formed.")
        return jsonify({"error": f"Invalid SSML format: {str(e)}"}), 400
    except Exception as e:
        print(f"An unexpected error occurred during SSML parsing or parameter extraction: {e}")
        return jsonify({"error": f"Failed to parse SSML or extract content: {str(e)}"}), 500

    try:
        # Use the extracted and transformed parameters to generate the TTS
        # The first argument is the actual speech content, not the full SSML.
        communicate = edge_tts.Communicate(
            extracted_content,
            voice=extracted_voice,
            rate=extracted_rate,
            pitch=extracted_pitch
        )
        
        audio_stream = BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_stream.write(chunk["data"])
        
        audio_stream.seek(0)

        return send_file(
            audio_stream,
            mimetype="audio/mpeg",
            as_attachment=True,
            download_name=output_filename
        )

    except Exception as e:
        print(f"Error generating TTS: {e}")
        # Provide more specific error if voice selection was the issue
        if "voice" in str(e).lower() and extracted_voice:
             return jsonify({"error": f"Failed to generate TTS: The voice '{extracted_voice}' might not be valid or available. Error: {str(e)}"}), 500
        return jsonify({"error": f"Failed to generate TTS: {str(e)}"}), 500

if __name__ == '__main__':
    # Ensure asyncio is available for edge_tts.Communicate
    app.run(debug=True, port=5000)
