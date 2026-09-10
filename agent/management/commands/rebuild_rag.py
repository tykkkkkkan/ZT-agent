"""重建知识库 RAG 索引（清空缓存并从 DB 重新构建）。

用法：
    python manage.py rebuild_rag

场景：
    - 在后台批量修改知识库后，手动触发索引重建（也可由 admin save/delete 自动失效）
    - 容器/进程重启前，确保索引与 DB 一致
"""
from django.core.management.base import BaseCommand

from agent.knowledge_data import get_knowledge_rag, invalidate_rag_cache


class Command(BaseCommand):
    help = "重建知识库 RAG 索引（清空缓存并从 DB 重新构建）"

    def handle(self, *args, **options):
        self.stdout.write("正在失效旧索引并重建 ...")
        invalidate_rag_cache()
        engine = get_knowledge_rag()
        n = len(engine.chunks)
        self.stdout.write(self.style.SUCCESS(f"RAG 索引重建完成，共 {n} 个切块。"))
