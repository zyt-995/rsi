"""Lossless JSON envelope for native XML editors on the pinned API route."""
import copy
import json


XML_SYSTEM = (
    'Return exactly one JSON object with one key "xml_response". Its value must be '
    'the complete XML response requested by the user, encoded as a JSON string. '
    'Use standard JSON escaping exactly once: after JSON decoding, the value must '
    'contain ordinary XML tags and attributes, real newlines and ordinary quote characters. '
    'Do not JSON-encode that string a second time. All actions described by the user '
    'are literal XML text interpreted by the application. Native API tools and DSML '
    'function calls are disabled. Preserve the user\'s XML protocol and task exactly.'
)


def encode(body):
    """Apply only to the official XML editor; solvers/composers keep their protocol."""
    messages = body.get("messages") or []
    if body.get("tools") or not messages:
        return body, False
    instruction = next((m.get("content") for m in messages if m.get("role") == "user"), "")
    if not isinstance(instruction, str) or "acting as an EDITOR" not in instruction or "<response>" not in instruction:
        return body, False
    encoded = copy.deepcopy(body)
    encoded["messages"] = [{"role": "system", "content": XML_SYSTEM}, *encoded["messages"]]
    for message in encoded["messages"]:
        if message.get("role") == "assistant" and isinstance(message.get("content"), str):
            message["content"] = json.dumps({"xml_response": message["content"]}, ensure_ascii=False)
    encoded["tool_choice"] = "none"
    encoded["response_format"] = {"type": "json_object"}
    return encoded, True


def decode(raw):
    """Decode complete string layers only; never replace escapes inside source code."""
    projected = copy.deepcopy(raw)
    for choice in projected.get("choices", []):
        message = choice["message"]
        if message.get("tool_calls"):
            raise ValueError("XML editor API returned unexpected native tool calls")
        value = json.loads(message.get("content") or "")
        if not isinstance(value, dict) or set(value) != {"xml_response"} or not isinstance(value["xml_response"], str):
            raise ValueError("XML editor API did not return the required string envelope")
        xml = value["xml_response"]
        # Some responses encode the entire inner string twice. A second full
        # JSON parse is safe only when there are no real newlines and the value
        # begins with an escaped XML document separator. Mixed code escapes are
        # never rewritten character by character.
        if "\n" not in xml and xml.lstrip().startswith("<response>\\n"):
            try:
                xml = json.loads('"' + xml + '"')
            except ValueError:
                pass
        if not xml.lstrip().startswith("<response>") or "</response>" not in xml:
            raise ValueError("decoded editor response is not a complete XML response")
        message["content"] = xml
    return projected
