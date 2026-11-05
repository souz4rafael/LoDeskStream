import math


MAX_TEXT_LENGTH = 4096
MAX_SDP_LENGTH = 128 * 1024


class ValidationError(ValueError):
    pass


def integer(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValidationError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def boolean(value, name):
    if type(value) is not bool:
        raise ValidationError(f"{name} must be a boolean")
    return value


def text(value, name, maximum):
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise ValidationError(f"{name} must be a non-empty string of at most {maximum} characters")
    return value


def coordinate(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{name} must be a finite number from 0 to 1")
    try:
        valid = math.isfinite(value) and 0 <= value <= 1
    except OverflowError:
        valid = False
    if not valid:
        raise ValidationError(f"{name} must be a finite number from 0 to 1")
    return float(value)


def validate_input(payload, supported_keys, translate_key):
    result = dict(payload)
    event_type = result.get("type")
    if not isinstance(event_type, str) or event_type not in {
        "mouse_move", "mouse_click", "mouse_down", "mouse_up", "scroll", "key", "text"
    }:
        raise ValidationError("Unknown input event type")
    if "monitorIndex" in result:
        integer(result["monitorIndex"], "monitorIndex", 1, 2147483647)
    if event_type in {"mouse_move", "mouse_click"} or (
        event_type in {"mouse_down", "mouse_up"} and ("x" in result or "y" in result)
    ):
        result["x"] = coordinate(result.get("x"), "x")
        result["y"] = coordinate(result.get("y"), "y")
    if event_type in {"mouse_down", "mouse_up", "mouse_click"}:
        result["button"] = integer(result.get("button", 0), "button", 0, 2)
    if event_type == "mouse_click":
        result["double"] = boolean(result.get("double", False), "double")
        result["preserveCursor"] = boolean(result.get("preserveCursor", False), "preserveCursor")
    if event_type == "scroll":
        result["amount"] = integer(result.get("amount"), "amount", -1000, 1000)
    if event_type == "key":
        key = translate_key(text(result.get("key"), "key", 64))
        if key not in supported_keys:
            raise ValidationError("Unsupported key")
        action = result.get("action", "tap")
        if not isinstance(action, str) or action not in {"down", "up", "tap"}:
            raise ValidationError("Key action must be down, up or tap")
        result["key"] = key
        result["action"] = action
    if event_type == "text":
        result["text"] = text(result.get("text"), "text", MAX_TEXT_LENGTH)
        if any(character.lower() not in supported_keys for character in result["text"]):
            raise ValidationError("Text contains unsupported characters; Unicode text input is not supported yet")
    return result


def validate_offer(payload):
    result = dict(payload)
    if result.get("type") != "offer":
        raise ValidationError("WebRTC type must be offer")
    sdp = text(result.get("sdp"), "sdp", MAX_SDP_LENGTH)
    if not sdp.startswith("v=0") or "\nm=video " not in sdp:
        raise ValidationError("Invalid video SDP offer")
    if "audio" in result:
        boolean(result["audio"], "audio")
    if "monitor" in result:
        integer(result["monitor"], "monitor", 1, 2147483647)
    return result
