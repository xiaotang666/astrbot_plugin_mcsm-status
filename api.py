"""
MCSManager Web API 客户端
封装所有与 MCSManager 面板的 HTTP 通信
基于 MCSManager v10 Web API
"""

import aiohttp
from typing import Optional, List, Tuple
from astrbot.api import logger


class McsmApiClient:
    """MCSManager API 客户端，负责与面板通信"""

    def __init__(self, base_url: str, api_key: str, timeout: int = 10):
        # MCSManager 面板地址（如 http://127.0.0.1:23333）
        self.base_url: str = base_url.rstrip("/")
        # MCSManager API Key（面板 → 用户设置 → API 接口）
        self.api_key: str = api_key
        self.timeout: int = timeout
        self._session: Optional[aiohttp.ClientSession] = None

    # ═══════════════════════════════════════════
    #  HTTP 客户端
    # ═══════════════════════════════════════════

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
        return self._session

    async def _api(self, method: str, path: str, params: dict = None, json_data: dict = None) -> dict:
        """
        发送 API 请求，自动添加 apikey 查询参数。
        """
        session = await self._ensure_session()
        url = f"{self.base_url}{path}"

        # 统一添加 apikey 到查询参数
        query_params = {"apikey": self.api_key}
        if params:
            query_params.update(params)

        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "X-Requested-With": "XMLHttpRequest"
        }

        try:
            async with session.request(
                method=method.upper(),
                url=url,
                params=query_params,
                json=json_data,
                headers=headers
            ) as resp:
                if resp.status != 200:
                    # 尝试解析错误信息
                    try:
                        err_json = await resp.json()
                        err_msg = err_json.get("error") or err_json.get("message") or str(err_json)
                    except:
                        err_msg = await resp.text()
                    raise RuntimeError(f"API 错误 [{resp.status}]: {err_msg[:200]}")

                return await resp.json()

        except aiohttp.ClientError as e:
            raise aiohttp.ClientError(f"连接 MCSManager 失败: {e}") from e

    # ═══════════════════════════════════════════
    #  核心 API 方法（符合 MCSManager v10）
    # ═══════════════════════════════════════════

    async def get_overview(self) -> dict:
        """获取面板概览信息（节点列表、系统信息等）"""
        return await self._api("GET", "/api/overview")

    async def get_remote_services(self) -> list:
        """获取所有远程守护进程（节点）列表"""
        resp = await self._api("GET", "/api/service/remote_services")
        return resp.get("data", []) if isinstance(resp, dict) else []

    async def get_remote_service_instances(self, daemon_id: str, page: int = 1, page_size: int = 100) -> list:
        """获取指定节点下的实例列表（分页）"""
        resp = await self._api(
            "GET",
            "/api/service/remote_service_instances",
            params={"daemonId": daemon_id, "page": page, "page_size": page_size}
        )
        data = resp.get("data", {}) if isinstance(resp, dict) else {}
        return data.get("data", []) if isinstance(data, dict) else data

    async def get_instance_info(self, daemon_id: str, instance_uuid: str) -> dict:
        """获取实例详细信息（使用 protected_instance/info）"""
        resp = await self._api(
            "GET",
            "/api/protected_instance/info",
            params={"uuid": instance_uuid, "daemonId": daemon_id}
        )
        return resp.get("data", {}) if isinstance(resp, dict) else {}

    async def operate_instance(self, daemon_id: str, instance_uuid: str, action: str) -> dict:
        """
        对实例执行操作。
        action 可选: open, stop, restart, kill
        """
        if action not in ("open", "stop", "restart", "kill"):
            raise ValueError(f"不支持的操作: {action}")
        resp = await self._api(
            "GET",
            f"/api/protected_instance/{action}",
            params={"uuid": instance_uuid, "daemonId": daemon_id}
        )
        return resp if isinstance(resp, dict) else {}

    async def send_command(self, daemon_id: str, instance_uuid: str, command: str) -> dict:
        """向运行中的实例发送控制台命令"""
        resp = await self._api(
            "GET",
            "/api/protected_instance/command",
            params={"uuid": instance_uuid, "daemonId": daemon_id, "command": command}
        )
        return resp if isinstance(resp, dict) else {}

    async def get_output_log(self, daemon_id: str, instance_uuid: str) -> str:
        """获取实例的最新日志输出"""
        resp = await self._api(
            "GET",
            "/api/protected_instance/outputlog",
            params={"uuid": instance_uuid, "daemonId": daemon_id}
        )
        return resp.get("data", "") if isinstance(resp, dict) else ""

    # ═══════════════════════════════════════════
    #  兼容原接口的封装（与 main.py 对接）
    # ═══════════════════════════════════════════

    async def get_services(self) -> list:
        """获取所有节点（兼容旧名）"""
        return await self.get_remote_services()

    async def get_instances(self, daemon_uuid: str) -> list:
        """获取指定节点上的所有实例（兼容旧名）"""
        return await self.get_remote_service_instances(daemon_uuid, page_size=200)

    async def get_instance_detail(self, daemon_uuid: str, instance_uuid: str) -> dict:
        """获取实例详情，返回格式为 {'instance': {...}} 以兼容原有 main.py"""
        info = await self.get_instance_info(daemon_uuid, instance_uuid)
        # 保证返回值至少包含 'instance' 键
        return {"instance": info} if info else {"instance": {}}

    async def find_instances(self, query: str = "") -> List[Tuple[str, str, dict, str]]:
        """
        遍历所有节点，根据名称或 UUID 模糊匹配实例。
        query 为空时返回所有实例。
        精确匹配优先于模糊匹配。

        返回: [(daemon_uuid, instance_uuid, instance_data, daemon_label), ...]
        """
        results: List[Tuple[str, str, dict, str]] = []
        exact: List[Tuple[str, str, dict, str]] = []

        try:
            # 先获取节点列表（从 overview）
            overview = await self.get_overview()
            nodes = overview.get("data", {}).get("remote", [])
            if not nodes:
                logger.warning("MCSM: 未获取到任何节点")
                return []
        except Exception as e:
            logger.error(f"MCSM: 获取节点列表失败: {e}")
            return []

        for node in nodes:
            if not isinstance(node, dict):
                continue
            daemon_id = node.get("uuid")
            daemon_label = node.get("remarks") or node.get("ip") or "unknown"

            try:
                instances = await self.get_remote_service_instances(daemon_id, page_size=200)
            except Exception as e:
                logger.warning(f"MCSM: 获取节点 {daemon_label} 实例失败: {e}")
                continue

            for inst in instances:
                if not isinstance(inst, dict):
                    continue
                cfg = inst.get("config", {})
                nickname = cfg.get("nickname", "")
                inst_uuid = inst.get("instanceUuid", "")

                if not query:
                    results.append((daemon_id, inst_uuid, inst, daemon_label))
                elif query.lower() in nickname.lower() or query.lower() in inst_uuid.lower():
                    results.append((daemon_id, inst_uuid, inst, daemon_label))
                    if query.lower() == nickname.lower() or query.lower() == inst_uuid.lower():
                        exact.append((daemon_id, inst_uuid, inst, daemon_label))

        return exact if exact else results

    # ═══════════════════════════════════════════
    #  生命周期
    # ═══════════════════════════════════════════

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None