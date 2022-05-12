# DenseRetrieval을 위한 BM25 코드

import json
import os
import pickle
import time
from typing import List, NoReturn, Optional, Tuple, Union
import faiss
import numpy as np
import pandas as pd
from tqdm.auto import tqdm
from utils_retrieval import timer
from rank_bm25 import BM25Okapi, BM25Plus
import time
from contextlib import contextmanager
from datasets import Dataset, concatenate_datasets, load_from_disk
from RetrievalBase import Base


class BM25_PLUS(Base):
    def __init__(
        self,
        tokenizer,
        tokenize_fn,
        data_path: Optional[str] = "/opt/ml/input/data/",
        caching_path = "caching/",
        context_path: Optional[str] = "wikipedia_documents.json",
    ) -> NoReturn:
        
        super().__init__(
            tokenizer,
            data_path=data_path,
            caching_path=caching_path,
            context_path=context_path,
        )
        self.wiki_text = self.wiki_dataset["text"]
        self.wiki_id = self.wiki_dataset["document_id"]
        self.wiki_title = self.wiki_dataset["title"]
        self.data_path = data_path
        with open(os.path.join(data_path, context_path), "r", encoding="utf-8") as f:
            wiki = json.load(f)
        
        print(f"Lengths of unique contexts : {len(self.context2id_dict)}")
        self.tokenize_fn = tokenize_fn

        self.bm25 = None  # get_sparse_embedding()로 생성합니다
    def get_sparse_embedding(self) -> NoReturn:

        """
        Summary:
            Passage Embedding을 만들고
            TFIDF와 Embedding을 pickle로 저장합니다.
            만약 미리 저장된 파일이 있으면 저장된 pickle을 불러옵니다.
        """

        # Pickle을 저장합니다.
        pickle_name = f"bm25.bin"
        emd_path = os.path.join(self.data_path, pickle_name)

        if os.path.isfile(emd_path):
            with open(emd_path, "rb") as file:
                self.bm25 = pickle.load(file)
            print("Embedding pickle load.")
        else:
            print("Build passage embedding")
            # BM25 instance 생성
            tokenized_corpus = [self.tokenize_fn(title + text) for title, text in zip(self.wiki_title, self.wiki_text)]
            # self.bm25 = BM25Okapi(tokenized_corpus)
            self.bm25 = BM25Plus(tokenized_corpus)
            with open(emd_path, "wb") as file:
                pickle.dump(self.bm25, file)
            print("Embedding pickle saved.")

    def retrieve(
        self, query_or_dataset: Union[str, Dataset], topk: Optional[int] = 1
    ) -> Union[Tuple[List, List], pd.DataFrame]:

        """
        Arguments:
            query_or_dataset (Union[str, Dataset]):
                str이나 Dataset으로 이루어진 Query를 받습니다.
                str 형태인 하나의 query만 받으면 `get_relevant_doc`을 통해 유사도를 구합니다.
                Dataset 형태는 query를 포함한 HF.Dataset을 받습니다.
                이 경우 `get_relevant_doc_bulk`를 통해 유사도를 구합니다.
            topk (Optional[int], optional): Defaults to 1.
                상위 몇 개의 passage를 사용할 것인지 지정합니다.
        Returns:
            1개의 Query를 받는 경우  -> Tuple(List, List)
            다수의 Query를 받는 경우 -> pd.DataFrame: [description]
        Note:
            다수의 Query를 받는 경우,
                Ground Truth가 있는 Query (train/valid) -> 기존 Ground Truth Passage를 같이 반환합니다.
                Ground Truth가 없는 Query (test) -> Retrieval한 Passage만 반환합니다.
        """

        assert self.bm25 is not None, "get_sparse_embedding() 메소드를 먼저 수행해줘야합니다."

        if isinstance(query_or_dataset, str):
            doc_scores, doc_indices = self.get_relevant_doc(query_or_dataset, k=topk)
            print("[Search query]\n", query_or_dataset, "\n")

            for i in range(topk):
                print(f"Top-{i+1} passage with score {doc_scores[i]:4f}")
                print(self.wiki_text[doc_indices[i]])

            return (doc_scores, [self.wiki_text[doc_indices[i]] for i in range(topk)])

        elif isinstance(query_or_dataset, Dataset):

            # Retrieve한 Passage를 pd.DataFrame으로 반환합니다.
            total = []
            with timer("query exhaustive search with bm25"):
                doc_scores, doc_indices = self.get_relevant_doc_bulk(
                    query_or_dataset["question"], k=topk
                )
            for idx, example in enumerate(
                tqdm(query_or_dataset, desc="Sparse retrieval with bm25 : ")
            ):
                tmp = {
                    # Query와 해당 id를 반환합니다.
                    "question": example["question"],
                    "id": example["id"],
                    # Retrieve한 Passage의 id, context를 반환합니다.
                    "context_id": doc_indices[idx], # 리스트 아닌가?
                    "context": " ".join(
                        [self.wiki_text[pid] for pid in doc_indices[idx]]
                    ),
                }
                if "context" in example.keys() and "answers" in example.keys():
                    # validation 데이터를 사용하면 ground_truth context와 answer도 반환합니다.
                    tmp["original_context"] = example["context"]
                    tmp["answers"] = example["answers"]
                total.append(tmp)

            cqas = pd.DataFrame(total) # correct qa
            return cqas

    def get_relevant_doc(self, query: str, k: Optional[int] = 1) -> Tuple[List, List]:

        """
        Arguments:
            query (str):
                하나의 Query를 받습니다.
            k (Optional[int]): 1
                상위 몇 개의 Passage를 반환할지 정합니다.
        Note:
            vocab 에 없는 이상한 단어로 query 하는 경우 assertion 발생 (예) 뙣뙇?
        """

        with timer("transform"):
            query_scores = self.bm25.get_scores(query)
        assert (
            np.sum(query_scores) != 0
        ), "오류가 발생했습니다. 이 오류는 보통 query에 vectorizer의 vocab에 없는 단어만 존재하는 경우 발생합니다."

        sorted_score = np.sort(query_scores)[::-1]
        sorted_id = np.argsort(query_scores)[::-1]
        doc_score = sorted_score[:k]
        doc_indices = sorted_id[:k]
        return doc_score, doc_indices

    def get_relevant_doc_bulk(
        self, queries: List, k: Optional[int] = 1
    ) -> Tuple[List, List]:

        """
        Arguments:
            queries (List):
                하나의 Query를 받습니다.
            k (Optional[int]): 1
                상위 몇 개의 Passage를 반환할지 정합니다.
        Note:
            vocab 에 없는 이상한 단어로 query 하는 경우 assertion 발생 (예) 뙣뙇?
        """
        score_path = os.path.join(self.data_path, "BM25_score.bin")
        indice_path = os.path.join(self.data_path, "BM25_indice.bin")

        # Pickle 파일 존재 시에 불러오기
        if os.path.isfile(score_path) and os.path.isfile(indice_path):
            with open(score_path, "rb") as file:
                doc_scores = pickle.load(file)
            with open(indice_path, "rb") as file:
                doc_indices = pickle.load(file)
            print("Load BM25 pickle")
        else:
            print('Build BM25 pickle')
            doc_scores = []
            doc_indices = []
            for query in tqdm(queries):
                tokenized_query = self.tokenize_fn(query)
                query_scores = self.bm25.get_scores(tokenized_query)

                sorted_score = np.sort(query_scores)[::-1]
                sorted_id = np.argsort(query_scores)[::-1]

                doc_scores.append(sorted_score[:k])
                doc_indices.append(sorted_id[:k])
            assert (
                np.sum(doc_scores) != 0
            ), "오류가 발생했습니다. 이 오류는 보통 query에 vectorizer의 vocab에 없는 단어만 존재하는 경우 발생합니다."

            with open(score_path, "wb") as f:
                pickle.dump(doc_scores,f)
            with open(indice_path, "wb") as f:
                pickle.dump(doc_indices,f)
            print("Load BM25 pickle")

        return doc_scores, doc_indices