from flask import Flask, render_template, request, redirect
import requests
import json
import openai
import re

app = Flask(__name__)

GPT_API_URL = "https://api.openai.com/v1/engines/davinci-codex/completions"
GPT_API_KEY = "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"


def is_uniprot_id(input_string):
    uniprot_id_pattern = re.compile(r"^[A-N,R-Z][0-9][A-Z][A-Z,0-9][A-Z][0-9]$")
    return bool(uniprot_id_pattern.match(input_string))

def get_protein_name(uniprot_id):
    UNIPROT_API_URL = f"https://www.uniprot.org/uniprot/{uniprot_id}.xml"
    response = requests.get(UNIPROT_API_URL)
    if response.status_code == 200:
        try:
            from xml.etree import ElementTree as ET
            tree = ET.fromstring(response.content)
            for entry in tree.iter("{http://uniprot.org/uniprot}entry"):
                for name in entry.iter("{http://uniprot.org/uniprot}fullName"):
                    return name.text
        except Exception as e:
            print(e)
            return None
    return None

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        query = request.form["query"]
        temperature = float(request.form["temperature"])
        number_of_results = float(request.form["number_of_results"])
        openai.api_key = GPT_API_KEY
        response = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=[
                    {
                        "role": "system",
                        "content": "You act as a biochemist. You will look into scientific literatures and look for potential protein binding partners for a protein. You will also evaluate the confidence of the binding interaction based on the literatures."
                    },
                    {
                        "role": "user",
                        "content": f"Find potential protein binding partners for a protein named {query}. List the names of top {number_of_results} protein binder candidates, a condifence score (ranging 0-100) on how likely you think the binding is true, the biological function of the protein binder, the possible biological funcion of the binding interaction, and your reasoning on why this is a true binder. Make sure that your output follows this format: each candidate protein is reported in one row, in each row you separate the name of the protein binder, a condifence score (ranging 0-100) on how likely you think the binding is true, the function of the protein binder, the possible funcion of the binding interaction, and your reasoning on why this is a true binder by a semicolon (you only need to give the result themselves and do not need to explain what they are). Do not give a numbered index for each row. No extra comments are needed."
                    }
                ],
                max_tokens=500,
                temperature=temperature
                )

        purification_protocol = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=[
                    {
                        "role": "system",
                        "content": "You act as a biochemical protocol generater. You will create a brief, step-by-step protocol to express and purify a protein. You will choose the optimal expression system and optimal purification methods based on the origin and physicochemical properties of the protein for the protocol."
                    },
                    {
                        "role": "user",
                        "content": f"Generate a brief, step-by-step protocol to express and purify {query}. Please choose the optimal expression system and optimal purification methods based on the origin and physicochemical properties of the protein for the protocol. Please just give the protocol and do not make other comments."
                    }
                ],
                max_tokens=2000,
                temperature=temperature
                )
        choices_text = [choice.strip() for choice in response['choices'][0]['message']['content'].strip().split("\n")[0:]]
        purification_protocol_text = purification_protocol['choices'][0]['message']['content'].strip().split("\n")
        return render_template("results.html", query=query, choices=choices_text, purification_protocol=purification_protocol_text)
    else:
        return render_template("index.html", error=None)

if __name__ == "__main__":
    app.run(debug=True)