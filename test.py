import requests


if __name__ == '__main__':
    # resp = requests.post(
    #     'http://127.0.0.1:8000/api/chat',
    #     headers={'X-API-Password': '12345'},
    #     json={'messages': [{'role': 'user', 'content': 'Напиши на Python быструю сортировку'}]},
    #     timeout=600,
    # )
    resp =requests.post(
        'http://127.0.0.1:8000/api/image',
        headers={'X-API-Password': '12345'},
        json={
            "prompt": "закат над горами, фотореализм", "steps": 25
        }
    )
    print(resp.status_code)
    print(resp.json())