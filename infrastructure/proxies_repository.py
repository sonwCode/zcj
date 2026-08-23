from __future__ import annotations

from sqlmodel import Session, select

from core.db import ProxyModel, engine
from core.proxy_utils import infer_proxy_region, normalize_proxy_url
from domain.proxies import ProxyCreateCommand, ProxyRecord, ProxyUpdateCommand


def _to_record(model: ProxyModel) -> ProxyRecord:
    return ProxyRecord(
        id=int(model.id or 0),
        url=model.url,
        region=model.region,
        success_count=model.success_count,
        fail_count=model.fail_count,
        is_active=bool(model.is_active),
        last_checked=model.last_checked,
    )


class ProxiesRepository:
    def list(self) -> list[ProxyRecord]:
        with Session(engine) as session:
            items = session.exec(select(ProxyModel)).all()
            known_urls = {item.url for item in items}
            changed = False
            for item in items:
                normalized = normalize_proxy_url(item.url)
                if normalized and normalized != item.url and normalized not in known_urls:
                    known_urls.discard(item.url)
                    item.url = normalized
                    known_urls.add(normalized)
                    session.add(item)
                    changed = True
                inferred_region = infer_proxy_region(normalized or item.url)
                if not item.region and inferred_region:
                    item.region = inferred_region
                    session.add(item)
                    changed = True
            if changed:
                session.commit()
                for item in items:
                    session.refresh(item)
            return [_to_record(item) for item in items]

    def get(self, proxy_id: int) -> ProxyRecord | None:
        with Session(engine) as session:
            model = session.get(ProxyModel, proxy_id)
            return _to_record(model) if model else None

    def create(self, command: ProxyCreateCommand) -> ProxyRecord | None:
        url = normalize_proxy_url(command.url)
        if not url:
            return None
        with Session(engine) as session:
            existing = session.exec(select(ProxyModel).where(ProxyModel.url == url)).first()
            if existing:
                return None
            model = ProxyModel(url=url, region=command.region.strip().upper() or infer_proxy_region(url))
            session.add(model)
            session.commit()
            session.refresh(model)
            return _to_record(model)

    def update(self, proxy_id: int, command: ProxyUpdateCommand) -> ProxyRecord | None:
        url = normalize_proxy_url(command.url)
        if not url:
            return None
        with Session(engine) as session:
            model = session.get(ProxyModel, proxy_id)
            if not model:
                return None
            existing = session.exec(select(ProxyModel).where(ProxyModel.url == url)).first()
            if existing and existing.id != proxy_id:
                return None
            model.url = url
            model.region = command.region.strip().upper() or infer_proxy_region(url)
            session.add(model)
            session.commit()
            session.refresh(model)
            return _to_record(model)

    def bulk_create(self, urls: list[str], region: str = "") -> int:
        added = 0
        normalized_region = region.strip().upper()
        with Session(engine) as session:
            seen: set[str] = set()
            for raw in urls:
                url = normalize_proxy_url(raw)
                if not url or url in seen:
                    continue
                seen.add(url)
                existing = session.exec(select(ProxyModel).where(ProxyModel.url == url)).first()
                if existing:
                    continue
                session.add(ProxyModel(url=url, region=normalized_region or infer_proxy_region(url)))
                added += 1
            session.commit()
        return added

    def delete(self, proxy_id: int) -> bool:
        with Session(engine) as session:
            model = session.get(ProxyModel, proxy_id)
            if not model:
                return False
            session.delete(model)
            session.commit()
            return True

    def toggle(self, proxy_id: int) -> bool | None:
        with Session(engine) as session:
            model = session.get(ProxyModel, proxy_id)
            if not model:
                return None
            model.is_active = not model.is_active
            session.add(model)
            session.commit()
            session.refresh(model)
            return bool(model.is_active)
