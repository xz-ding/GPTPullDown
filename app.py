import json
import os
import re

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_from_directory
from openai import OpenAI, OpenAIError


load_dotenv()

app = Flask(__name__)

MODEL_OPTIONS = [
    {"id": "gpt-5.5", "label": "GPT-5.5"},
    {"id": "gpt-5.4", "label": "GPT-5.4"},
    {"id": "gpt-5.4-mini", "label": "GPT-5.4 mini"},
]
ALLOWED_MODELS = {model["id"] for model in MODEL_OPTIONS}
DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.5")
if DEFAULT_MODEL not in ALLOWED_MODELS:
    DEFAULT_MODEL = "gpt-5.5"

MAX_QUERY_LENGTH = 200
MIN_RESULTS = 1
MAX_RESULTS = 5
DEFAULT_TEMPERATURE = 0.5


class ModelResponseError(Exception):
    pass


BINDER_INSTRUCTIONS = (
    "You are a knowledgeable and critical biochemist. Identify plausible "
    "protein binding partners from known biology and literature-level "
    "knowledge. Return only data that fits the supplied schema. Use confidence "
    "scores from 0 to 100, where higher means stronger evidence for direct or "
    "functionally relevant binding. If evidence is sparse, still return "
    "plausible low-confidence candidates when useful, and explain the "
    "uncertainty in the reasoning field. If the target is not a real protein "
    "or no useful candidates can be identified, return an empty binders array "
    "and put the reason in warning."
)

BINDER_RESPONSE_FORMAT = {
    "type": "json_schema",
    "name": "binding_partner_results",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "binders": {
                "type": "array",
                "minItems": 0,
                "maxItems": MAX_RESULTS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "name": {"type": "string"},
                        "confidence_score": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 100,
                        },
                        "protein_function": {"type": "string"},
                        "interaction_function": {"type": "string"},
                        "reasoning": {"type": "string"},
                    },
                    "required": [
                        "name",
                        "confidence_score",
                        "protein_function",
                        "interaction_function",
                        "reasoning",
                    ],
                },
            },
            "warning": {"type": "string"},
        },
        "required": ["binders", "warning"],
    },
}

PURIFICATION_INSTRUCTIONS = (
    "You are a biochemical protocol generator. Create a brief step-by-step "
    "protein expression and purification protocol. Choose the expression "
    "system and purification methods based on the origin and physicochemical "
    "properties of the protein. Return only the protocol steps."
)


@app.route("/.well-known/pki-validation/<filename>")
def serve_dcv_file(filename):
    return send_from_directory(".well-known/pki-validation", filename)


def is_uniprot_id(input_string):
    uniprot_id_pattern = re.compile(r"^[A-NR-Z][0-9][A-Z][A-Z0-9][A-Z][0-9]$")
    return bool(uniprot_id_pattern.match(input_string))


def get_protein_name(uniprot_id):
    uniprot_api_url = f"https://www.uniprot.org/uniprot/{uniprot_id}.xml"
    response = requests.get(uniprot_api_url, timeout=10)
    if response.status_code == 200:
        try:
            from xml.etree import ElementTree as ET

            tree = ET.fromstring(response.content)
            for entry in tree.iter("{http://uniprot.org/uniprot}entry"):
                for name in entry.iter("{http://uniprot.org/uniprot}fullName"):
                    return name.text
        except Exception as exc:
            print(exc)
            return None
    return None


def clamp(value, lower, upper):
    return max(lower, min(value, upper))


def get_api_key():
    return os.environ.get("OPENAI_API_KEY") or os.environ.get("GPT_API_KEY")


def get_openai_client():
    api_key = get_api_key()
    if not api_key:
        raise RuntimeError("OpenAI API key is not configured.")
    return OpenAI(api_key=api_key)


def parse_prediction_form(form):
    query = form.get("query", "").strip()
    if not query:
        raise ValueError("Enter a target protein name.")
    if len(query) > MAX_QUERY_LENGTH:
        raise ValueError(f"Target protein name must be {MAX_QUERY_LENGTH} characters or fewer.")

    try:
        temperature = float(form.get("temperature", DEFAULT_TEMPERATURE))
    except (TypeError, ValueError) as exc:
        raise ValueError("Wildness must be a number between 0 and 1.") from exc
    temperature = clamp(temperature, 0.0, 1.0)

    try:
        number_of_results = int(form.get("number_of_results", MAX_RESULTS))
    except (TypeError, ValueError) as exc:
        raise ValueError("Number of hits must be a whole number from 1 to 5.") from exc
    number_of_results = clamp(number_of_results, MIN_RESULTS, MAX_RESULTS)

    model = form.get("model", DEFAULT_MODEL)
    if model not in ALLOWED_MODELS:
        raise ValueError("Choose a supported GPT model.")

    return query, temperature, number_of_results, model


def parse_binding_response(response_text, number_of_results):
    try:
        payload = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise ModelResponseError("The model returned an invalid response format.") from exc

    raw_binders = payload.get("binders", [])
    if not isinstance(raw_binders, list):
        raise ModelResponseError("The model returned an invalid binder list.")

    binders = []
    for binder in raw_binders[:number_of_results]:
        if not isinstance(binder, dict):
            continue
        try:
            confidence_score = int(binder.get("confidence_score", 0))
        except (TypeError, ValueError):
            confidence_score = 0
        binders.append(
            {
                "name": str(binder.get("name", "")).strip(),
                "confidence_score": clamp(confidence_score, 0, 100),
                "protein_function": str(binder.get("protein_function", "")).strip(),
                "interaction_function": str(binder.get("interaction_function", "")).strip(),
                "reasoning": str(binder.get("reasoning", "")).strip(),
            }
        )

    warning = str(payload.get("warning", "")).strip()
    return binders, warning


def get_binding_partners(query, temperature, number_of_results, model, client=None):
    client = client or get_openai_client()
    response = client.responses.create(
        model=model,
        instructions=BINDER_INSTRUCTIONS,
        input=(
            f"Target protein: {query}\n"
            f"Return exactly {number_of_results} candidate binders unless evidence is insufficient."
        ),
        max_output_tokens=500 + number_of_results * 250,
        temperature=temperature,
        reasoning={"effort": "low"},
        text={"format": BINDER_RESPONSE_FORMAT, "verbosity": "low"},
        store=False,
    )
    return parse_binding_response(response.output_text, number_of_results)


def render_index(error=None, form=None, status_code=200):
    form = form or {}
    selected_model = form.get("model", DEFAULT_MODEL)
    if selected_model not in ALLOWED_MODELS:
        selected_model = DEFAULT_MODEL
    return (
        render_template(
            "index.html",
            error=error,
            model_options=MODEL_OPTIONS,
            selected_model=selected_model,
            query_value=form.get("query", "SARS-Cov-2 Spike RBD"),
            temperature_value=form.get("temperature", DEFAULT_TEMPERATURE),
            number_of_results_value=form.get("number_of_results", MAX_RESULTS),
        ),
        status_code,
    )


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        try:
            query, temperature, number_of_results, model = parse_prediction_form(request.form)
            binders, warning = get_binding_partners(query, temperature, number_of_results, model)
        except ValueError as exc:
            return render_index(str(exc), request.form, 400)
        except RuntimeError as exc:
            return render_index(str(exc), request.form, 500)
        except OpenAIError:
            return render_index(
                "OpenAI could not complete the request. Please try again later.",
                request.form,
                502,
            )
        except ModelResponseError as exc:
            return render_index(str(exc), request.form, 502)

        return render_template(
            "results.html",
            query=query,
            binders=binders,
            warning=warning,
            temperature=temperature,
            model=model,
        )

    return render_index()


@app.route("/get_purification_protocol", methods=["POST"])
def get_purification_protocol():
    try:
        query = request.form.get("query", "").strip()
        if not query:
            raise ValueError("Enter a target protein name.")
        if len(query) > MAX_QUERY_LENGTH:
            raise ValueError(f"Target protein name must be {MAX_QUERY_LENGTH} characters or fewer.")

        try:
            temperature = float(request.form.get("temperature", DEFAULT_TEMPERATURE))
        except (TypeError, ValueError) as exc:
            raise ValueError("Wildness must be a number between 0 and 1.") from exc
        temperature = clamp(temperature, 0.0, 1.0)

        client = get_openai_client()
        response = client.responses.create(
            model=DEFAULT_MODEL,
            instructions=PURIFICATION_INSTRUCTIONS,
            input=f"Generate a brief expression and purification protocol for {query}.",
            max_output_tokens=2000,
            temperature=temperature,
            reasoning={"effort": "low"},
            text={"verbosity": "medium"},
            store=False,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500
    except OpenAIError:
        return jsonify({"error": "OpenAI could not complete the request."}), 502

    return jsonify([line for line in response.output_text.strip().splitlines() if line.strip()])


if __name__ == "__main__":
    app.run(debug=True)
