import logging
from datasets import Dataset
from transformers import AutoTokenizer
from src.dataset.preprocess import tokenize_dataset

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ds = Dataset.from_dict({'text':['Bonjour le monde','Salut'], 'label':[0,1]})
tok = AutoTokenizer.from_pretrained('distilbert-base-multilingual-cased')
out = tokenize_dataset(ds, tok, max_length=8)
logger.info(out.column_names)
logger.info(out[0])
logger.info(type(out[0]))
