import json


class AttrDict(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def __getattr__(self, item):
        if item in self:
            return self.__getitem__(item)
        return super().__getattribute__(item)


def is_json_serializable(data, function_name):
    try:
        json.dumps(data)
    except TypeError:
        raise TypeError(f"{function_name} output is not JSON serializable!")


def verify_url_exists(endpoint_url: str) -> bool:
    import urllib.request
    from urllib.request import HTTPError
    from urllib.error import URLError

    try:
        urllib.request.urlopen(
            urllib.request.Request(endpoint_url),
            timeout=50)
    except HTTPError as e:
        # verify that the connection is not authorized, anything else would mean the endpoint is failed
        return e.code == 403
    except URLError:
        # Endpoint does not exist - skip the test
        return False
    raise Exception("Task Succeeded unexpectedly.")
