from transformers import AutoModelForSequenceClassification


def build_model(cfg):
    model = AutoModelForSequenceClassification.from_pretrained(
        cfg["model_name"],
        num_labels=3,
    )
    return model
