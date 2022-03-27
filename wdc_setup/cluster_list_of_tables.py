import argparse
import json
import numpy as np
import re
import time
from collections import Counter
from pathlib import Path
from sklearn.cluster import KMeans, MiniBatchKMeans
from spacy.language import Language
from tqdm import tqdm

np.random.seed(0)

if __name__=='__main__':

    parser = argparse.ArgumentParser()
    parser.add_argument("--input_dir", type=str, required=True)
    parser.add_argument("--num_clusters", type=int, default=5000)
    parser.add_argument("--spacy_vec_file", type=str, default='/home/jc11431/.cache/spacy/crawl-300d-2M.vec')
    args = parser.parse_args()

    # Load string representations of all the files we want to cluster
    jsonl_paths = [str(p) for p in Path(args.input_dir).glob("**/*.jsonl")]
    table_strings = [] # These will be converted into embeddings for clustering
    print("Getting string representations of jsonl tables...")
    for jsonl_file in tqdm(jsonl_paths):
        with open(jsonl_file) as f:
            line = f.readline()
            table_string = json.loads(line)['table_title']
            table_strings.append(table_string)

    # Use Spacy to load Vectors
    # !pip install spacy
    # !wget https://dl.fbaipublicfiles.com/fasttext/vectors-english/crawl-300d-2M.vec.zip
    # !unzip crawl-300d-2M.vec.zip
    spacy_vec_file = Path(args.spacy_vec_file)
    assert spacy_vec_file.exists(), f"{spacy_vec_file} does not exist!"
    nlp = Language()
    print('[*] Loading Vectors with Spacy...')
    with open(spacy_vec_file, "rb") as f:
        header = f.readline()
        nr_row, nr_dim = header.split()
        nlp.vocab.reset_vectors(width=int(nr_dim))
        for line in tqdm(f, total=2000000):
            line = line.rstrip().decode("utf8")
            pieces = line.rsplit(" ", int(nr_dim))
            word = pieces[0]
            vector = np.asarray([float(v) for v in pieces[1:]], dtype="f")
            nlp.vocab.set_vector(word, vector)

    # Embed each table into a vector
    def get_embedding(string):
        vector = nlp(string).vector
        return vector / np.linalg.norm(vector)

    embedding_vecs = np.array([get_embedding(string) for string in table_strings])
    print('Dataset Embedding Matrix Shape:', embedding_vecs.shape)

    # Assign each embedding to a cluster
    num_clusters = args.num_clusters
    print(f"Clustering {len(jsonl_paths)} files into {num_clusters} clusters...")
    start_t = time.time()
    kmeans = MiniBatchKMeans(n_clusters=num_clusters, batch_size=2048, max_iter=20)
    kmeans.fit(embedding_vecs)
    distances = kmeans.transform(embedding_vecs)
    clusters = kmeans.predict(embedding_vecs) # contains [cluster_id for vec in embedding_vecs]
    print(f"Done in {time.time() - start_t}s.")
    
    # Cluster elements into list of clusters (list of list of elements)
    train_files = []
    total_examples = 0
    clustered_paths = []
    cluster_names = []
    for cluster_id in range(num_clusters):
        members_of_this_cluster = list(np.array(jsonl_paths)[clusters == cluster_id])
        clustered_paths.append(members_of_this_cluster)

        # Form the cluster name from the top-5 words in the cluster
        strings_of_this_cluster = list(np.array(table_strings)[clusters == cluster_id])
        cluster_string = ' '.join(strings_of_this_cluster)
        cluster_string = re.sub('[^a-zA-Z]+', ' ', cluster_string) # Letters and spaces only
        counter = Counter((cluster_string).split())
        top_5_wordscounts = sorted(counter.items(), key=lambda pair: pair[1], reverse=True)[:5]
        top_5_words = [word for word, count in top_5_wordscounts]
        cluster_names.append('_'.join(top_5_words))
    assert sum([len(cluster) for cluster in clustered_paths]) == len(jsonl_paths)

    # Save output
    out_dir = Path(args.input_dir).parent / f"{Path(args.input_dir).stem}-cluster{num_clusters}"
    out_dir.mkdir(parents=True, exist_ok=True)

    def save_config_file(filepath, train_files):
        test_tasks = ["piqa", "hate_speech_offensive", "google_wellformed_query", "social_i_qa", "circa", "glue-sst2", "scitail", "emo", "cosmos_qa", "ag_news", "art", "paws", "glue-qnli", "quail", "ade_corpus_v2-classification", "sciq", "hatexplain", "emotion", "glue-qqp", "kilt_fever"]
        task_config = {
            "train": train_files,
            "test": test_tasks,
        }
        with open(filepath, 'w') as f:
            json.dump(task_config, f, indent=4, sort_keys=True)
        return

    # Save one member of each cluster to a config file
    train_files = [np.random.choice(cluster) for cluster in clustered_paths if len(cluster)]
    output_file = out_dir / f"cluster{num_clusters}_sampleeach.json"
    save_config_file(output_file, train_files)
    print(f"Saved {len(train_files)} samples to {output_file}. ({num_clusters - len(train_files)} / {num_clusters} empty)")

    # Save all clusters
    all_clusters_file = out_dir / f"allclusters{num_clusters}.txt"
    with open(all_clusters_file, 'w') as f:
        for cluster_id, (cluster, cluster_name) in enumerate(zip(clustered_paths, cluster_names)):
            # Write to common file for easy browsing
            print(f"\nCLUSTER {cluster_id}: {cluster_name} ({len(cluster)} tables)", file=f)
            for path in cluster:
                print(path, file=f)

            # Write each cluster to its own file
            single_cluster_file = out_dir / f"cluster{num_clusters}_idx{cluster_id}_{cluster_name}.json"
            save_config_file(single_cluster_file, cluster)