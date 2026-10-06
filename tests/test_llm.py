import llm


def test_first_json_extracts_dict_from_surrounding_text() -> None:
    result = llm.first_json('prefix {"intent":"其他","todos":[]} suffix')
    assert result == {"intent": "其他", "todos": []}


def test_first_json_skips_malformed_object_before_valid_dict() -> None:
    result = llm.first_json('bad {not json} then {"ok":true}')
    assert result == {"ok": True}


def test_first_json_returns_empty_dict_without_object() -> None:
    assert llm.first_json("plain text") == {}
