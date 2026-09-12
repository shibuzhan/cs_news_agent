"""兼容入口：实际受控采集实现仍由 source_tools 提供。"""

from app.tools.source_tools import SourceCollectionTool, ToolValidationError, build_source_tools

__all__ = ["SourceCollectionTool", "ToolValidationError", "build_source_tools"]
