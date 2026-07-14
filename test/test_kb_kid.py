from store.knowledge_base import KnowledgeTable, KnowledgeUnit

def main():
    dim = 4
    kb = KnowledgeTable(dim=dim)

    kb.upsert_kid("a"*64, KnowledgeUnit(embedding=[1,0,0,0], length=10))
    kb.upsert_kid("b"*64, KnowledgeUnit(embedding=[0,1,0,0], length=10))
    kb.build_faiss_index()

    res = kb.search_by_embedding([1,0,0,0], topk=1)
    print(res[0][0])  # should be "a"*64

if __name__ == "__main__":
    main()
