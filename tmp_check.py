from datasets import Dataset
from transformers import AutoTokenizer
from src.dataset.preprocess import tokenize_dataset

ds = Dataset.from_dict({'text':['Bonjour le monde','Salut'], 'label':[0,1]})
tok = AutoTokenizer.from_pretrained('distilbert-base-multilingual-cased')
out = tokenize_dataset(ds, tok, max_length=8)
print(out.column_names)
print(out[0])
print(type(out[0]))
