from statistics import mean
import time
import subprocess
import sys
import requests
import json
import torch
import torch.optim as optim
from torch import nn
import pythonmonkey as pm
import glob
import os
import t3dqn as t3
import t3stats

api_base_url = "http://127.0.0.1:5000"
out_dir = "./data/training"
session_template = "training-{:06d}-{:06d}"
max_epochs = 500
max_sessions = 100

delete_training_files = True
# player_server_script = "player-server.py"
player_server_script = "no-server.py"
model_filename = "./data/model/t3-simple1.pt"
stats_filename = "./data/model/t3-stats.dat"


def wait_for_server(base_url):
    url = f"{base_url}/ping"
    for i in range(50):
        try:
            response = requests.get(url)
            body = response.json()
            # print(f'Health API response: {body}')

            if body["alive"]:
                return
            else:
                break

        except Exception as ex:
            # wait for a bit
            print(f'server exception: {ex}')
            time.sleep(5)
    #
    sys.exit(-1)


def reload_model(base_url):
    url = f"{base_url}/model/reload"
    response = requests.post(url)
    body = response.json()
    print(f"Reload API Response: {body}")


def run_games(epoch, out_dir, exploration_rate):
    for i in range(max_sessions):
        session = session_template.format(epoch, i)
        subprocess.run(["node", "../game/build/tic-tac-toe.console.js",
                        "--player1Type", "RLWebAgentPlayer",
                        "--player2Type", "RandomPlayer",
                        "--player1Profile", "rl-agent-1",
                        "--trueRandomRate", "0.5",
                        "--suppressOutput",
                        "--configDir", "../game/config",
                        "--outdir", out_dir,
                        "--sessionName", session,
                        # "--encoder", "BitEncoder",
                        "--explorationRate", f"{exploration_rate}"
                        ])


def calculate_reward(action, player, winner, turns_left):
    if not action["isValid"]:
        return -10

    if player == winner and turns_left == 1:
        return 1
    elif winner is not None and player != winner and turns_left == 2:
        return -1

    return 0


def calculate_session_stats(stats, winner, status_msg):
    if winner == 0:
        if "draw" in status_msg:
            stats["game_draws"] += 1
    elif winner is None:
        if status_msg == "Player1 disqualified!":
            stats["p1_dq"] += 1
        elif status_msg == "Player2 disqualified!":
            stats["p2_dq"] += 1
    elif winner == 1:
        stats["p1_wins"] += 1
    elif winner == 2:
        stats["p2_wins"] += 1
    return


def create_memories(memories, epoch, in_dir):
    # memory is a list of [state, action, reward, next_state]

    epoch_stats = {"p1_wins": 0, "p2_wins": 0, "p1_dq": 0, "p2_dq": 0, "game_draws": 0}
    for i in range(max_sessions):
        session = session_template.format(epoch, i)
        filename = in_dir + "/" + session.format(epoch, i) + ".txt"

        with open(filename) as file:
            parsed_json = json.load(file)

            if 'winner' in parsed_json:
                winner = parsed_json['winner']
            else:
                winner = 0

            calculate_session_stats(epoch_stats, winner, parsed_json['status'])

            history = parsed_json['history']
            valid_moves = list(filter(lambda turn: turn["isValid"], history))
            invalid_moves = list(filter(lambda turn: not turn["isValid"], history))

            max_turns = len(valid_moves)
            for curr_turn, action in enumerate(valid_moves):

                if "choice" not in action:
                    continue

                turns_left = max_turns - curr_turn
                reward = calculate_reward(action, action["player"], winner, turns_left)

                state = [action["player"], action["board"]]
                choice = action["choice"]

                if turns_left > 1:
                    next_action = valid_moves[curr_turn + 1]
                    next_state = [next_action["player"], next_action["board"]]
                else:
                    next_state = None
                memories.append([state, choice, reward, next_state])

            for action in invalid_moves:

                if "choice" not in action:
                    continue

                state = [action["player"], action["board"]]
                choice = action["choice"]
                reward = -10
                memories.append([state, choice, reward, None])
    return epoch_stats


def make_qlearning_train_step(policy_dqn, target_dqn, loss_fn, optimizer, discount_rate):
    def train_step(input, label, reward, next_input):

        # Sets model to TRAIN mode
        target_dqn.eval()
        policy_dqn.train()

        # Calculate Q Value
        if next_input is None:
            q_value = torch.tensor(reward)
        else:
            with torch.no_grad():
                # next state's reward is subtracted from current state instead of added
                # because next state is the other player's turn. Therefore, if the other
                # player made a successful move, then that is bad for the current player
                q_value = reward - (discount_rate * target_dqn(next_input).max())

        # Makes predictions
        y = policy_dqn(input)
        yhat = target_dqn(input)

        # Update target_dqn output with q_value
        yhat[label] = q_value.item()

        # Computes loss
        loss = loss_fn(y, yhat)

        # Computes gradients
        loss.backward()

        # Updates parameters and zeroes gradients
        optimizer.step()
        optimizer.zero_grad()

        # Returns the loss
        return loss.item()

    # Returns the function that will be called inside the train loop
    return train_step


def create_list(pylist, js_proxy_list):
    pylist *= 0
    for i in js_proxy_list:
        pylist.append(i)
    return


def train(model, step, memories, decoder=None, board_size=0):
    model.train()
    losses = []
    for action in memories:
        player, state = action[0]
        feature = torch.tensor(state + [player], dtype=torch.float)
        label = action[1]

        reward = action[2]

        next_state = action[3]
        next_input = None
        if next_state is not None:
            next_player, next_board = next_state
            next_input = torch.tensor(next_board + [next_player], dtype=torch.float)

        loss = step(input=feature, label=label, reward=reward, next_input=next_input)
        losses.append(loss)
        print(f"loss: {loss}")

    print(f"")
    return losses


def cleanup_files(file_dir, pattern):
    if delete_training_files:
        for f in glob.glob(f"{file_dir}/{pattern}"):
            os.remove(f)


def app():
    discovery_rate = 1.0
    decay_rate = 0.99

    learn_rate = 0.01
    discount_rate = 0.9
    policy_dqn = t3.get_model(filename=model_filename)

    target_dqn = t3.get_model()

    loss_fn = nn.MSELoss()
    optimizer = optim.Adam(policy_dqn.parameters(), lr=learn_rate)
    train_step = make_qlearning_train_step(policy_dqn, target_dqn, loss_fn, optimizer, discount_rate)

    board_size = 9
    encoder = pm.require("../game/src/bitencoder.js")

    wait_for_server(api_base_url)
    memories = []
    game_stats = t3stats.GameStats(max_epochs=max_epochs, max_sessions=max_sessions)

    for epoch in range(max_epochs):
        target_dqn.load_state_dict(policy_dqn.state_dict())
        run_games(epoch, out_dir, discovery_rate)

        memories *= 0
        epoch_stats = create_memories(memories, epoch, out_dir)
        epoch_stats["exploration_rate"] = discovery_rate
        print(f"memories={memories}")

        losses = train(model=policy_dqn, step=train_step, memories=memories, decoder=encoder, board_size=board_size)
        epoch_stats["avg_loss"] = mean(losses)

        t3.save_model(policy_dqn, filename=model_filename, archive=False)
        cleanup_files(out_dir, "training-*.txt")

        game_stats.add_epoch_stats(epoch_stats)
        t3stats.save_stats(stats_filename, game_stats)

        reload_model(api_base_url)
        discovery_rate = discovery_rate * decay_rate


# =========================
# Entry Point
# =========================
# with subprocess.Popen(["venv/Scripts/python", f"{player_server_script}"]) as proc:
#    app()
#    print("stopping proc")
#    proc.kill()

app()
