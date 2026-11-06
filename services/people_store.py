from config import PEOPLE_FILE
import storage


def _normalize(name: str) -> str:
    return name.strip().lower()


async def load_people() -> dict:
    return await storage.read_json(PEOPLE_FILE, {})


async def save_people(people: dict):
    await storage.write_json(PEOPLE_FILE, people)


def find_person_key(people: dict, name: str) -> str:
    target = _normalize(name)
    for key in people:
        if _normalize(key) == target:
            return key
    return name


def new_person_entry() -> dict:
    return {"discord_user_id": None, "history": []}
