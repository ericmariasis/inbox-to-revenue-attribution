from typing import Protocol

class EmailProvider(Protocol):
    def send_magic_link(self, email: str, token: str) -> None: ...