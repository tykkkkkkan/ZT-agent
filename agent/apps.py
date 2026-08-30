from django.apps import AppConfig


class AgentConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'agent'
    verbose_name = '中渔天下 Agent'

    def ready(self):
        # 注册知识库变更信号：后台编辑 KnowledgeChunk 时自动失效 RAG 索引缓存，
        # 下次请求重建，保证「改了知识库立即生效」（无需重启进程）。
        from django.db.models.signals import post_save, post_delete
        from django.dispatch import receiver
        from agent.knowledge_data import invalidate_rag_cache
        from agent.models import KnowledgeChunk

        @receiver([post_save, post_delete], sender=KnowledgeChunk)
        def _on_knowledge_chunk_change(sender, instance, **kwargs):
            invalidate_rag_cache()
