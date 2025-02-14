import os
import json
import time
import random
# fix seed
random.seed(42)
import logging
from datetime import datetime
# set to most verbose logging level
import os

log_dir = "experiments/article_annotation/logs"
os.makedirs(log_dir, exist_ok=True)

now = datetime.now()
logging.basicConfig(level=logging.INFO, filename=f"{log_dir}/{now.strftime('%Y-%m-%d_%H-%M-%S')}.log", filemode='w')
logging.getLogger().addHandler(logging.StreamHandler())
import argparse
import concurrent.futures
import pandas as pd
import ast
from tqdm import tqdm

from dotenv import load_dotenv
from openai import AzureOpenAI
from nltk.tokenize import sent_tokenize

def load_json_data() -> pd.DataFrame:
    """
    Load all jsonl files in the data directory and returns a dataframe
    :param data_dir: the path to the directory containing the json files (one per clas)
    :return: the data as a pd.Dataframe
    """
    data_dir = os.path.join("training/datasets/annotated_data")
    rows = []
    for f in [f for f in os.listdir(data_dir) if f.endswith('jsonl')]:
        with open(f'{data_dir}/{f}', 'r') as json_file:
            json_list = list(json_file)
        for json_str in json_list:
            result = json.loads(json_str)
            label = result["label"]
            text = result["text"]
            new_spans = []
            if result["spans"] and len(result["spans"]) != 0:
                try:
                    new_spans = [s["text"] for s in result["spans"]]
                except:
                    x = ast.literal_eval(result["spans"])
                    new_spans = [s["text"] for s in x]
            new_row = {
                "text": text,
                "label": label,
                "spans": new_spans,
                "article_id": result["meta"]["doc_id"],
                "sentence_id": result["meta"]["sent_id"]
            }
            rows.append(new_row)
        logging.info(f"Loaded: {f}")
    df = pd.DataFrame(rows)
    # only keep rows with unique article_id and sentence_id
    df.drop_duplicates(subset=["article_id", "sentence_id"], inplace=True)
    return df

# Set seed for random number generator
seed = 42
random.seed(seed)

# Set logging level
logging.basicConfig(level=logging.INFO)
logging.getLogger('httpx').setLevel(logging.WARNING)

# Load environment variables
print(os.getcwd())
load_dotenv("experiments/.env")

# Initialize OpenAI client
OpenaiClient = AzureOpenAI(
    azure_endpoint=f"https://{os.environ.get('AZURE_OPENAI_SERVICE') or 'aiforgood-openai'}.openai.azure.com",
    api_key=os.getenv("AZURE_OPENAI_API_KEY"),
    api_version="2024-02-15-preview"
)

# Initialize global variables
AZURE_OPENAI_MODEL_NAME = os.environ.get("AZURE_OPENAI_MODEL_NAME") or "gpt-35-turbo"
AZURE_OPENAI_DEPLOYMENT_NAME = os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME") or "gpt-35-turbo"
TOTAL_COST = 0
TOTAL_NUM_REQUESTS = 0
TOKENS_PER_MINUTE = 0
TOTAL_TOKENS = 0
START_TIME = None

# Initialize argument parser
parser = argparse.ArgumentParser(description='Article Labeling using ChatGPT 2.0')
parser.add_argument('--num-annotators', type=int, default=2, help='Number of annotators to simulate')
parser.add_argument('--patience', type=int, default=3, help='Number of rounds of evaluation before evaluator gives up')
parser.add_argument('--results_dir', type=str, default='experiments/article_annotation/results', help='Directory to save the results')
parser.add_argument('--testing', action='store_true', help='Whether to run in testing mode')
args = parser.parse_args()

# Annotation System Prompt
ANNOTATION_SYSTEM_PROMPT = '''
As an expert in article/news classification, your task is to categorize sections of news articles into specific topics. The available categories include artificial intelligence, autonomous transport, sustainable fabrics, and more. Keep in mind that articles may cover multiple topics, but a category should be chosen based on the primary focus. For example, if artificial intelligence is a supporting element, it should not be the main category. The available categories are: 
<categories>
{categories}
</categories>
'''

# Annotation User Prompt
ANNOTATION_USER_PROMPT = '''
Please categorize the following article segment by selecting the most appropriate category from the provided list. Respond only with the category name. If uncertain or if the section lacks relevant information for any category, reply with "unsure." Include specific keywords you believe support your decision. If you selected "unsure," please extract various keywords and entities from the section.
<article-title>{title}</article-title>
<section>
{text}
</section>
'''

# Evaluation System Prompt
EVALUATION_SYSTEM_PROMPT = '''
As an expert in article/news category classification within an annotation taskforce, your role is to review the reasoning behind your fellow annotators' category choices. Evaluate whether the reasoning justifies the category choice. Consider specific keywords or phrases that indicate the category. Directly respond to the annoator with your feedback. You may respond with "valid" if you agree with the annotator's reasoning, and "invalid" if you disagree.
The available categories are:
<categories>
{categories}
</categories>
If an annotator chose "unsure" due to a lack of relevant information, consider it a valid choice. If the any of the information provided by the annotator is "None" or missing information, immediately respond with "invalid". If the category choice is not part of the available categories, respond with "invalid".
'''

# Evaluation User Prompt
EVALUATION_USER_PROMPT = '''
Please review my reasoning behind the category choice and provide me with feedback on whether it's valid. Respond with "valid" if the reasoning justifies my category choice, and "invalid" if it doesn't. Do not use any other responses. Please also provide a brief explanation for your decision.
<chosen-category>{category}</chosen-category>
<annotator-reasoning>{reasoning}</annotator-reasoning>
<keywords>{keywords}</keywords>
'''

# Decision System Prompt
DECISION_SYSTEM_PROMPT = '''
As an expert in article/news classification within an annotation taskforce, your task is to make the final decision on the category choice for given article sections. You will be provided with three category choices made by fellow annotators, along with their reasoning. Choose the category that best represents the section. If none are suitable, respond with "none."  Important: provide a brief explanation for your decision and elborate on why the other categories are not as well-suited.
'''

# Decision User Prompt
DECISION_USER_PROMPT = '''
Review the category choices made by your fellow annotators and select the one that best represents the section. If none are appropriate, respond with "none." Do not use any other responses. Provide a brief explanation for your decision and why the other categories are not as well-suited.
<article-title>{title}</article-title>
<section>
{text}
</section>
{annotator_results}
'''

def get_tools(categories):
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_section_annotation",
                "description": "Categorize the article section into one of the listed categories.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "category": {
                            "type": "string",
                            "enum": categories + ["unsure"],
                            "description": "The name of the category that best represents the section. If you are unsure or if you find that the section contains no relevant information for any category, select 'unsure'.",
                        },
                        "reasoning": {
                            "type": "string",
                            "description": "The reasoning for your decision. Provide a brief but clear explanation for why you chose the selected category. If you selected 'unsure', explain why, spare no details, and include various extracted keywords and entities."
                        },
                        "keywords": {
                            "type": "array",
                            "items": {
                                "type": "string"
                            },
                            "description": "Specific keywords or entities from the article section that support your decision. If you selected 'unsure', please extract various keywords and entities from the section."
                        }
                    },
                    "required": ["category", "reasoning", "keywords"]
                },
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_annotation_evaluation",
                "description": "Review the reasoning behind the category choice and provide feedback on whether the reasoning is valid or invalid.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "conclusion": {
                            "type": "string",
                            "enum": ["valid", "invalid"],
                            "description": "Whether the reasoning is valid and justifies the category choice. Respond with 'valid' if you AGREE with the annotator, and 'invalid' if you DO NOT AGREE. Respond with 'invalid' if the category choice is not part of the available categories"
                        },
                        "reasoning": {
                            "type": "string",
                            "description": "The reasoning for your conclusion. Provide a brief, but clear explanation for why you decided that the annotator's reasoning is valid or invalid."
                        }
                    },
                    "required": ["conclusion", "reasoning"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_final_annotation_decision",
                "description": "Make the final decision on the category choice for the given article section.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "category": {
                            "type": "string",
                            "enum": categories + ["none"],
                            "description": "The name of the category chosen by your fellow annotators that best represents the section. If you believe that none of the category choices are appropriate, select 'none'."
                        },
                        "reasoning": {
                            "type": "string",
                            "description": "The reasoning for your decision. Provide a brief, but clear explanation for why you chose the selected category and for why the other categories are not as well-suited. If you selected 'none', please explain why, spare no details."
                        }
                    },
                    "required": ["category", "reasoning"]
                }
            }
        }
    ]
    return tools


def load_raw_articles(filename):
    """
    This function loads raw articles from a given JSONL file.

    Parameters:
    filename (str): The path to the JSONL file containing the articles.

    Returns:
    list: A list of dictionaries where each dictionary represents an article.

    The function opens the file, reads it line by line (each line is a JSON string),
    converts each line to a Python dictionary using json.loads() and appends it to a list.
    The list of all articles is returned.
    """
    articles = []
    with open(filename) as file:
        json_list = list(file)
        for json_str in json_list:
            article = json.loads(json_str)
            articles.append(article)
    return articles


def preprocess_articles(articles):
    """
    This function preprocesses the articles.

    Parameters:
    articles (list): A list of dictionaries where each dictionary represents an article.

    Returns:
    tuple: A tuple containing the preprocessed articles and all unique categories.

    The function performs the following preprocessing steps:
    1. Filters out articles that do not have a "climate_scanner" field.
    2. Extracts all unique categories from the articles and formats them by replacing certain characters.
    3. Removes duplicate articles based on their title.
    4. Reassigns the id of each article based on its index in the list.
    5. Saves the preprocessed articles to a JSONL file.
    """
    articles = [a for a in articles if a["climate_scanner"]]
    for a in articles:
        a["category"] = a["category"].lower().replace(" ", "_").replace("-", "_").replace("&", "and").replace("/", "_").replace("4", "for").replace("(", "").replace(")", "")
    all_categories = list(set([a["category"] for a in articles]))
    # Remove duplicate articles based on their title
    articles = [article for idx, article in enumerate(articles) if article["title"] not in [a["title"] for a in articles[idx + 1:]]]
    # Reassign the id of each article based on its index in the list
    for idx, article in enumerate(articles):
        article["old_id"] = article["id"]
        article["id"] = idx
    # Save the preprocessed articles to a JSONL file
    with open(f'{args.results_dir}/data_used_for_annotation_with_chatgpt.jsonl', 'w') as outfile:
        for article in articles:
            json.dump(article, outfile)
            outfile.write('\n')
    return articles, all_categories


def split_articles_into_sections(article, section_length=3):
    """
    Splits the text of an article into sections.

    Parameters:
    article (dict): A dictionary representing an article with keys "text" and "id".
    section_length (int): The number of sentences per section. Default is 3.

    Returns:
    list: A list of dictionaries, each representing a section of the article.
    """
    tokens = sent_tokenize(article["text"])
    sections = [tokens[i:i + section_length] for i in range(0, len(tokens), section_length)]
    sections = [" ".join(s) for s in sections]
    sections_formatted = [{"text": s, "article_id": article["id"], "old_article_id":article["old_id"], "section_id": idx, "title": article["title"]} for idx, s in enumerate(sections)]
    return sections_formatted


def cost_decorator(func):
    """
        A decorator function to calculate the cost of using an OpenAI model.

        This decorator wraps around a function that makes a request to an OpenAI model and calculates the cost of that request based on the number of tokens used and the specific model used.

        Parameters:
        func (function): The function that makes a request to an OpenAI model.

        Returns:
        function: The wrapper function that calculates the cost and updates global variables accordingly.
        """
    def wrapper(*args, **kwargs):
        global TOTAL_COST
        global TOTAL_NUM_REQUESTS
        global TOKENS_PER_MINUTE
        global TOTAL_TOKENS
        usage, assistant_message, tool_call = func(*args, **kwargs)
        try:
            prompt_tokens = usage.prompt_tokens
            completion_tokens = usage.completion_tokens
        except AttributeError:
            pass
        if AZURE_OPENAI_MODEL_NAME == "gpt-35-turbo":
            cost = prompt_tokens / 1000 * 0.0005 + completion_tokens / 1000 * 0.0015
        elif AZURE_OPENAI_MODEL_NAME == "gpt-4":
            cost = prompt_tokens / 1000 * 0.03 + completion_tokens / 1000 * 0.06
        else:
            raise ValueError("Model not supported")
        TOTAL_COST += cost
        TOTAL_NUM_REQUESTS += 1
        TOTAL_TOKENS += prompt_tokens + completion_tokens
        minutes_since_start = (datetime.now() - START_TIME).total_seconds() / 60
        TOKENS_PER_MINUTE = int(TOTAL_TOKENS / minutes_since_start) if minutes_since_start > 1 else TOTAL_TOKENS
        return assistant_message, tool_call
    return wrapper


@cost_decorator
def get_response(messages, tools=None, tool_choice=None):
    """
    A decorator function to calculate the cost of using an OpenAI model.

    This decorator wraps around a function that makes a request to an OpenAI model and calculates the cost of that request based on the number of tokens used and the specific model used.

    Parameters:
    func (function): The function that makes a request to an OpenAI model.

    Returns:
    function: The wrapper function that calculates the cost and updates global variables accordingly.
    """
    patience = 3
    current_patience = 0
    e = None
    while True:
        try:
            response = OpenaiClient.chat.completions.create(
                model=AZURE_OPENAI_MODEL_NAME,
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,

            )
            assistant_message = response.choices[0].message
            tool_calls = assistant_message.tool_calls
            if tool_calls:
                tool_call = tool_calls[0]
                # check if tool_call.function.arguments is valid json
            else:
                raise ValueError("No tool calls in assistant message.")
            # check if tool_call.function.arguments is valid json
            json.loads(tool_call.function.arguments)
            return response.usage, assistant_message, tool_call
        except Exception as ex:
            if current_patience >= patience:
                raise ValueError("Exceeded patience limit. Failed to get response.") from ex
            else:
                logging.error(f"Error in get_response: {ex}")
                logging.info(f"Retrying request. Patience: {current_patience + 1}/{patience}")
        current_patience += 1


def evaluate_response(assistant_message, tool_call, conversation, tools, category_group):
    """
    Evaluates the response from the OpenAI API.

    This function takes the response from the OpenAI API, extracts the assistant's message and tool calls,
    and parses the arguments of the tool call based on the function name.

    Parameters:
    response (openai.ChatCompletion): The response from the OpenAI API.

    Returns:
    tuple: A tuple containing a dictionary of the parsed response and a list of response messages.

    Raises:
    ValueError: If the function name in the tool call is not supported.
    """
    
    assistant_message.content = str(assistant_message.tool_calls[0].function)
    response_messages = [{"role": assistant_message.role, "content": assistant_message.content}]
    response_messages.append(
        {"role": "function", "tool_call_id": tool_call.id, "name": tool_call.function.name,
         "content": tool_call.function.arguments}
    )
    if tool_call.function.name == "get_section_annotation":
        parsed_response = {
                "category": json.loads(tool_call.function.arguments).get("category"),
                "reasoning": json.loads(tool_call.function.arguments).get("reasoning"),
                "keywords": json.loads(tool_call.function.arguments).get("keywords")
            }
        
        if parsed_response["category"] not in category_group + ["unsure"]:
            patience = 1
            logging.info (f"Invalid category choice: {parsed_response['category']}")
            while True and patience < 3:
                parsed_response = redo_because_of_invalid_category(conversation+response_messages, tools, category_group)
                patience += 1
            logging.info(f"New category choice: {parsed_response['category']}")
    elif tool_call.function.name == "get_annotation_evaluation":
        parsed_response = {
                "conclusion": json.loads(tool_call.function.arguments).get("conclusion"),
                "reasoning": json.loads(tool_call.function.arguments).get("reasoning")
            }
    elif tool_call.function.name == "get_final_annotation_decision":
        parsed_response = {
                "category": json.loads(tool_call.function.arguments).get("category"),
                "reasoning": json.loads(tool_call.function.arguments).get("reasoning")
            }
    else:
        raise ValueError("Tool call not supported")
    return parsed_response, response_messages


def redo_because_of_invalid_category(conversation_with_annotator, tools, category_group):
    """
    Redo the conversation with the annotator because of an invalid category choice.

    This function appends a message to the conversation with the annotator to inform them that their category choice is invalid and asks them to review their decision.

    Parameters:
    conversation_with_annotator (list): A list of messages in the conversation with the annotator.
    """
    conversation_with_annotator.append({
        "role": "user",
        "content": "You chose a category that is not part of the available categories. Please review your decision and choose a category from the list provided. If you chose '3d_printed_fashion' or similar, please consider '3d_printed_apparel' instead."
    })
    _, tool_call = get_response(
        conversation_with_annotator,
        tools=tools,
        tool_choice=tools[0]
        )
    parsed_response = {
        "category": json.loads(tool_call.function.arguments).get("category"),
        "reasoning": json.loads(tool_call.function.arguments).get("reasoning"),
        "keywords": json.loads(tool_call.function.arguments).get("keywords")
    }
    return parsed_response


def evaluate_section(section, category_groups):
    """
    Evaluates a section of an article using multiple category groups.

    This function simulates a conversation with multiple annotators and evaluators to categorize and evaluate a section of an article (see evaluate_category_group).
    It repeats the process until the evaluator finds the annotator's reasoning sound or the patience limit is reached.

    Parameters:
    section (dict): A dictionary representing a section of an article.
    category_groups (list): A list of category groups. Each group is a list of categories that the annotator can choose from.

    Returns:
    dict: A dictionary containing the final decision, which includes the section id, the chosen category, the reasoning, and an explanation.
    """
    def evaluate_category_group(category_group, group_num, section, tools):
        """
        Evaluates a category group for a given section of an article.

        This function simulates a conversation with an annotator and an evaluator to categorize and evaluate a section of an article.
        It repeats the process until the evaluator finds the annotator's reasoning sound or the patience limit is reached.

        Parameters:
        category_group (list): A list of categories that the annotator can choose from.
        group_num (int): The group number of the category group.
        section (dict): A dictionary representing a section of an article.
        tools (list): A list of tool objects to be sent to the OpenAI API.

        Returns:
        tuple: A tuple containing the final prediction, a dictionary of the chain of results, and the group number.
        """
        categories_enumerated = "\n".join([f"{idx + 1}: {category}" for idx, category in enumerate(category_group)])
        chain_of_results = {}
        conversation_with_annotator = [
            {"role": "system", "content": ANNOTATION_SYSTEM_PROMPT.format(categories=categories_enumerated)},
            {"role": "user", "content": ANNOTATION_USER_PROMPT.format(title=section["title"], text=section["text"])}
        ]
        cg_level_pred, annotator_response = evaluate_response(
            *get_response(
                conversation_with_annotator,
                tools=tools,
                tool_choice={"type": "function", "function": {"name": tools[0]["function"]["name"]}}
            ),
            conversation_with_annotator,
            tools,
            category_group
        )
        chain_of_results["prediction_1"] = cg_level_pred
        conversation_with_evaluator = [
            {"role": "system", "content": EVALUATION_SYSTEM_PROMPT.format(categories=categories_enumerated)},
            {"role": "user", "content": EVALUATION_USER_PROMPT.format(
                category=cg_level_pred["category"],
                reasoning=cg_level_pred["reasoning"],
                keywords=cg_level_pred["keywords"])
             }
        ]
        evaluation, evaluator_response = evaluate_response(
            *get_response(
                conversation_with_evaluator,
                tools=tools,
                tool_choice={"type": "function", "function": {"name": tools[1]["function"]["name"]}}
            ),
            conversation_with_evaluator,
            tools,
            category_group
        )
        chain_of_results["evaluation_1"] = evaluation
        finished = evaluation["conclusion"] == "valid"
        current_round = 1
        while not finished and current_round < args.patience:
            conversation_with_annotator += annotator_response
            conversation_with_annotator.append({"role": "user", "content": f"Sorry, but I don't agree with you. Given your reasoning, I found your category choice questionable. {evaluation['reasoning']} Please review your decision."})
            cg_level_pred, annotator_response = evaluate_response(
                *get_response(
                    conversation_with_annotator,
                    tools=tools,
                    tool_choice={"type": "function", "function": {"name": tools[0]["function"]["name"]}}
                ),
                conversation_with_annotator,
                tools,
                category_group
            )
            conversation_with_evaluator += evaluator_response
            conversation_with_evaluator.append({"role": "user", "content": f"I have given your evaluation some thought. {cg_level_pred['reasoning']} As a result I chose this category: {cg_level_pred['category']}."})
            evaluation, evaluator_response = evaluate_response(
                *get_response(
                    conversation_with_evaluator,
                    tools=tools,
                    tool_choice={"type": "function", "function": {"name": tools[1][ "function"]["name"]}}
                ),
                conversation_with_evaluator,
                tools,
                category_group
            )
            finished = evaluation["conclusion"] == "valid"
            current_round += 1
            chain_of_results[f"prediction_{current_round}"] = cg_level_pred
            chain_of_results[f"evaluation_{current_round}"] = evaluation
        return cg_level_pred, chain_of_results, group_num

    predictions_from_annotators = []
    chains_of_results = {}
    with concurrent.futures.ThreadPoolExecutor() as executor:
        future_to_category_group = {
            executor.submit(evaluate_category_group, group, group_num, section, get_tools(group)): group for group_num, group in enumerate(category_groups)
        }
        for future in concurrent.futures.as_completed(future_to_category_group):
            prediction, chain_of_results, group_num = future.result()
            predictions_from_annotators.append(prediction)
            chains_of_results[group_num] = chain_of_results
    annotators_results = "\n".join([f"<choice-annotator_{idx + 1}>\n<category>{p['category']}</category>\n<reasoning>{p['reasoning']}</reasoning>\n</choice-annotator_{idx + 1}>" for idx, p in enumerate(predictions_from_annotators)])
    conversation_with_final_decision_maker = [
        {"role": "system", "content": DECISION_SYSTEM_PROMPT},
        {"role": "user", "content": DECISION_USER_PROMPT.format(
            title=section["title"],
            text=section["text"],
            annotator_results=annotators_results
        )}
    ]
    tools = get_tools([p["category"] for p in predictions_from_annotators])
    final_decision, final_decision_response = evaluate_response(
        *get_response(
            conversation_with_final_decision_maker, 
            tools=tools,
            tool_choice={"type": "function", "function": {"name": tools[2]["function"]["name"]}}
            ),
        conversation_with_final_decision_maker,
        tools,
        [p["category"] for p in predictions_from_annotators]
    )
    if final_decision["category"] not in [p["category"] for p in predictions_from_annotators] + ["none"]:
        logging.warning(f"Final decision not among annotators' choices: {final_decision['category']}")
        conversation_with_final_decision_maker += final_decision_response
        conversation_with_final_decision_maker.append({"role": "user", "content": f"The category you chose is not among the categories chosen by the annotators. Your task is to choose one of the categories chosen by the annotators or 'none' if none of the annotators choices are suitable."})
        final_decision, final_decision_response = evaluate_response(
            *get_response(conversation_with_final_decision_maker, 
                         tools=tools,
                         tool_choice={"type": "function", "function": {"name": tools[2]["function"]["name"]}}
            ),
            conversation_with_final_decision_maker,
            tools,
            [p["category"] for p in predictions_from_annotators]
        )
    final_decision["section_id"] = section["section_id"]
    final_decision["text"] = section["text"]
    final_decision["explanation"] = chains_of_results
    return final_decision


def run(sections_by_article, categories):
    """
    Runs the article labelling process.

    This function divides the categories among annotators, evaluates each section of each article, and tracks the progress and cost of the process.

    Parameters:
    sections_by_article (dict): A dictionary where each key is an article id and the value is a list of sections in that article.
    categories (list): A list of all unique categories.

    Yields:
    dict: A dictionary containing the predictions for each article.

    The function performs the following steps:
    1. Divides the categories among the annotators.
    2. Iterates over each article and its sections.
    3. Evaluates each section using the evaluate_section function and appends the prediction to the predictions list.
    4. After all sections of an article have been evaluated, it yields the predictions for that article.
    """
    # round up the number of categories per annotator
    num_categories_per_annotator = len(categories) / args.num_annotators
    num_categories_per_annotator = int(num_categories_per_annotator) + 1 if num_categories_per_annotator % 1 > 0 else int(num_categories_per_annotator)
    category_groups = [categories[i:i + num_categories_per_annotator] for i in
                       range(0, len(categories), num_categories_per_annotator)]
    logging.info(f"Number of categories in groups (one per annotator): {[len(group) for group in category_groups]}")
    total_sections = sum([len(sections) for sections in sections_by_article.values()])
    sections_done = 0
    for article_id, article_sections in sections_by_article.items():
        logging.info(f"Starting article ID: {article_id}")
        predictions = {article_id: []}
        for idx, section in enumerate(article_sections):
            try:
                prediction = evaluate_section(section, category_groups)
                predictions[article_id].append(prediction)
            except Exception as e:
                # check if e has 'code' attribute
                if hasattr(e.__cause__, 'code') and e.__cause__.code == "content_filter":
                    logging.warning(f"Content filter triggered. Skipping section {idx+1} in article ID: {article_id}")
                    continue
            if TOKENS_PER_MINUTE > 250000:
                logging.warning("Approaching token limit reached. Pausing for 1 minute.")
                time.sleep(60)
            sections_done += 1
            logging.info(f"Finished section {idx+1} out of {len(article_sections)}")
        logging.info(f"Finished article ID: {article_id}, %Done: {sections_done / total_sections:.2%}")
        logging.info(f"Current: total cost: ${TOTAL_COST:.6f}, tokens per minute: {TOKENS_PER_MINUTE:.2f}, total tokens: {TOTAL_TOKENS}, total number of requests: {TOTAL_NUM_REQUESTS}, average tokens per request: {TOTAL_TOKENS / TOTAL_NUM_REQUESTS:.2f}")
        yield predictions
    logging.info(f"Final Total cost: ${TOTAL_COST:.6f}")
    logging.info(f"Final Total tokens: {TOTAL_TOKENS}")
    logging.info(f"Final Tokens per minute: {TOKENS_PER_MINUTE:.2f}")
    logging.info(f"Final Total number of requests: {TOTAL_NUM_REQUESTS}")


def init_run(sections_by_article, outfile_path, skip_user_input=False):
    """
    Initializes the article evaluation process.

    This function checks if previous predictions exist and asks the user if they want to overwrite them.
    If the user chooses to overwrite, it deletes the existing predictions file.
    If the user chooses not to overwrite, it resumes the process from the last evaluated article.

    Parameters:
    sections_by_article (dict): A dictionary where each key is an article id and the value is a list of sections in that article.

    Returns:
    dict: A dictionary containing the sections of the articles to be evaluated.
    """
    previous_predictions_exist = os.path.exists(outfile_path)
    if previous_predictions_exist and not skip_user_input:
        i = input("Predictions already exist. Do you want to overwrite them? (y/n): ")
        if i.lower() == "y":
            i = input("Are you sure? (y/n): ")
            if i.lower() == "y":
                os.remove(outfile_path)
                logging.info("Will overwrite existing predictions.")
                previous_predictions_exist = False
            else:
                logging.info("Will append to existing predictions.")
        else:
            logging.info("Will append to existing predictions.")
    if previous_predictions_exist:
        # get last article id
        with open(outfile_path, 'r') as file:
            lines = file.readlines()
            # get the id from the json string. the string starts with {"<ID>": ...}, get it by regex
            import re
            last_article_id = int(re.search(r'{"(\d+)":', lines[-1]).group(1))
            #last_line = json.loads(lines[-1])
            #last_article_id = int(list(last_line.keys())[0])
            sections_by_article = {article_id: sections for article_id, sections in sections_by_article.items() if
                                   article_id > last_article_id}
            logging.info("Resuming from article id: %s", last_article_id + 1)
    return sections_by_article


def create_test_dataset_from_annotated(raw_articles):
    old_data = load_json_data()
    # sort by article id
    old_data = old_data.sort_values(by="article_id")
    data = {}
    data_annotations = {}
    for article_id in old_data.article_id.unique():
        article_rows = old_data[old_data.article_id == article_id]
        # get raw article from list of raw articles where old_id == article_id
        raw_article = [article for article in raw_articles if article["old_id"] == article_id]
        if len(raw_article) == 0:
            logging.warning(f"No raw article found for article id: {article_id}")
            continue
        else:
            raw_article = raw_article[0]
        data[article_id] = []
        data_annotations[article_id] = []
        for _, row in article_rows.iterrows():
            data[article_id].append({
                "text": row.text,
                "article_id": article_id,
                "section_id": row["sentence_id"],
                "title": raw_article["title"]
            })
            data_annotations[article_id].append(row.label)
    # dump data_annotations to json file
    with open(f'{args.results_dir}/test_run/test_data_annotations.json', 'w') as outfile:
        json.dump({str(k): v for k, v in data_annotations.items()}, outfile)
    return data


def main():
    raw_articles = load_raw_articles('data/raw_data.jsonl')
    raw_articles, categories = preprocess_articles(raw_articles)
    if args.testing:
        # /test_run
        now = datetime.now()
        outfile_path = args.results_dir + f"/test_run/predictions_{now.strftime('%Y-%m-%d_%H-%M-%S')}.jsonl"
        sections_by_article = create_test_dataset_from_annotated(raw_articles)
    else:
        outfile_path = args.results_dir + '/predictions.jsonl'
        sections_by_article = {article["id"]: split_articles_into_sections(article, section_length=3) for article in raw_articles}
        # filter already annotated articles 
        annotaded_articles_ids = [int(k) for k in load_json_data().old_article_id.unique()]
        logging.info(f"Number of articles already annotated: {len(annotaded_articles_ids)}")
        sections_by_article = {k: v for k, v in sections_by_article.items() if k not in annotaded_articles_ids}
    sections_by_article = init_run(sections_by_article, outfile_path)
    logging.info(f"Total number of articles/sections to annotate: {len(raw_articles)}/{sum([len(sections) for sections in sections_by_article.values()])}")
    random.shuffle(categories)
    logging.info("Categories: %s", categories)
    for predictions in run(sections_by_article, categories):
        logging.info("Writing predictions to file.")
        
        with open(outfile_path, 'a') as outfile:
            to_write = {str(k): v for k, v in predictions.items()}
            try:
                json.dump(to_write, outfile)
                outfile.write('\n')
            except Exception as e:
                logging.error(f"Error writing predictions to file: {e}. ")


if __name__ == '__main__':
    START_TIME = datetime.now()
    main()