"""조달데이터허브 검색 엔진 (런타임).

mcp/ Cloudflare Worker 가 JS 로 재구현했던 것을 이 Python 구현으로 되돌렸다.
- search_hybrid : BM25(bm25s) + Vector(Jina) → 가중 RRF
- stage_solve   : S0~S5 결정적 스테이지 솔버
- slot_parser   : 쿼리 슬롯 파싱 (family/dims/metric/channel/visual)
- concept_tagger: 개념 확장
- catalog       : 131건 카탈로그 로더 + 공개용 이름 정리
- embed         : Jina 임베딩 클라이언트

주의: 이 패키지 모듈은 **상대 import 만** 쓴다. 평평한 top-level import 를 쓰면
sys.path 에 app/retrieval 이 함께 올라가면서 같은 파일이 app.retrieval.X 와 X 로
두 번 로드된다. 그 결과 BM25 인덱스가 메모리에 두 벌 올라가고, EmbeddingError 같은
예외 클래스가 서로 달라 except 가 빗나간다.

CLI 로 단독 실행할 때만(__main__) 자기 디렉터를 sys.path 에 넣는다.
"""
