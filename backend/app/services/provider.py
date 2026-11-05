from fastapi.encoders import jsonable_encoder
from kombu import uuid

from app.db.models.providers import Provider
from app.db.provider_dao import (
    insert_provider,
    get_all_providers,
    get_provider_by_name,
    get_provider_by_id,
    update_provider,
    delete_provider, get_enabled_providers,
)
from app.gpt.gpt_factory import GPTFactory
from app.models.model_config import ModelConfig
from app.exceptions.provider import ProviderNameExistsError


class ProviderService:

    @staticmethod
    def serialize_provider(row: Provider) -> dict:
        if not row:
            return None
        row = ProviderService.provider_to_dict(row)
        return {
            "id": row.get("id"),
            "name": row.get("name"),
            "logo": row.get("logo"),
            "type":row.get("type"),
            "enabled": row.get("enabled"),
            "base_url": row.get("base_url"),
            "api_key": row.get("api_key"),
            "max_concurrency": row.get("max_concurrency"),
            "max_rpm": row.get("max_rpm"),
            "created_at": jsonable_encoder(row.get("created_at")),
            # "name": row[1],
            # "logo": row[2],
            # "type": row[3],
            # "api_key": row[4],
            # "base_url": row[5],
            # "enabled": row[6],
            # "created_at": row[7],
        }
    @staticmethod
    def serialize_provider_safe(row: Provider) -> dict:
        if not row:
            return None
        row = ProviderService.provider_to_dict(row)

        return {
            "id": row.get("id"),
            "name": row.get("name"),
            "logo": row.get("logo"),
            "type":row.get("type"),
            "enabled": row.get("enabled"),
            "base_url": row.get("base_url"),
            "api_key":  ProviderService.mask_key(row.get("api_key")),
            "max_concurrency": row.get("max_concurrency"),
            "max_rpm": row.get("max_rpm"),
            "created_at": jsonable_encoder(row.get("created_at")),

            # "id": row[0],
            # "name": row[1],
            # "logo": row[2],
            # "type": row[3],
            # "api_key": ProviderService.mask_key(row[4]),
            # "base_url": row[5],
            # "enabled": row[6],
            # "created_at": row[7],
        }
    @staticmethod
    def mask_key(key: str) -> str:
        if not key or len(key) < 8:
            return '*' * len(key)
        return key[:4] + '*' * (len(key) - 8) + key[-4:]
    @staticmethod
    def add_provider( name: str, api_key: str, base_url: str, logo: str, type_: str, enabled: int = 1,
                      max_concurrency: int | None = None, max_rpm: int | None = None):
        try:
            # 内置供应商已移除（builtin_providers.json 不再存在），API 创建一律落到
            # 'custom'：历史上出现过批量伪内置脏数据，类型不该由请求方决定。
            if type_ != 'custom':
                type_ = 'custom'
            existing = get_provider_by_name(name)
            if existing is not None:
                raise ProviderNameExistsError(f'供应商名称已存在: {name}')
            id = uuid().lower()
            logo = 'custom'
            created = insert_provider(id, name, api_key, base_url, logo, type_, enabled)
            # 限流额度是后加的列，老调用点不传就保持数据库默认值（4 / 600）。
            limits = {}
            if max_concurrency is not None:
                limits['max_concurrency'] = max_concurrency
            if max_rpm is not None:
                limits['max_rpm'] = max_rpm
            if limits:
                update_provider(id, **limits)
            return created
        except Exception as  e:
            print('创建模式失败',e)
            raise
    @staticmethod
    def provider_to_dict(p: Provider):
        return {
            "id": p.id,
            "name": p.name,
            "logo": p.logo,
            "type": p.type,
            "api_key": p.api_key,
            "base_url": p.base_url,
            "enabled": p.enabled,
            "max_concurrency": p.max_concurrency,
            "max_rpm": p.max_rpm,
            "created_at": p.created_at,
        }
    @staticmethod
    def get_all_providers():
        rows = get_all_providers()
        if rows is None:
            return []

        return [ProviderService.serialize_provider(row) for row in rows] if rows else []
    @staticmethod
    def get_all_providers_safe():
        """列表接口一律返回打码后的 api_key。

        以前这里走的是 serialize_provider（明文），设置页因此拿得到所有供应商的真实
        key。打码后前端仍能展示与判断「有没有配 key」，而 update_provider 已经把带 `*`
        的值当作「不修改」丢弃，所以编辑流程不会用掩码覆盖真 key。
        """
        rows = get_all_providers()

        return [ProviderService.serialize_provider_safe(row) for row in rows] if (rows) else []
    @staticmethod
    def get_provider_by_name(name: str):
        row = get_provider_by_name(name)
        return ProviderService.serialize_provider(row)

    @staticmethod
    def get_provider_by_id(id: str):  # 已改为 str 类型
        row = get_provider_by_id(id)
        return ProviderService.serialize_provider(row)

    @staticmethod
    def get_provider_by_id_safe(id: str):  # 已改为 str 类型
        row = get_provider_by_id(id)
        return ProviderService.serialize_provider_safe(row)
            # all_models.extend(provider['models'])

    @staticmethod
    def update_provider(id: str, data: dict)->str | None:
        try:
        # 过滤掉空值
            filtered_data = {k: v for k, v in data.items() if v is not None and k != 'id'}
            # 防御掩码污染：前端展示时 api_key 被 mask_key() 处理过（如 a92f****...2d3a），
            # 如果用户未重新输入直接保存，带星号的值不应覆盖原 key。
            if 'api_key' in filtered_data and '*' in str(filtered_data.get('api_key', '')):
                filtered_data.pop('api_key')
            # Local deployment: never log credentials or provider payloads.
            update_provider(id, **filtered_data)
            # 获取更新后的供应商信息
            updated_provider = get_provider_by_id(id)
            return {
                'id': id,
                'enabled': updated_provider.enabled,
            }

        except Exception as e:
            print('更新模型供应商失败：',e)
            return None

    @staticmethod
    def delete_provider(id: str):
        return delete_provider(id)
