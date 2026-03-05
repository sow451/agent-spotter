# agent-spotter: context

## Table of Contents

- **About**: [Purpose](#purpose), [Inspired By](#inspired-by), [Core Hypothesis](#core-hypothesis)
- **Measurements**: [Data Points](#data-points), [Interpretation](#interpretation), [Flow](#flow), [Definitions](#definitions), [Privacy](#privacy), [Success Criteria](#success-criteria)
- **Other**: [Out of Scope](#out-of-scope), [Limitations](#limitations), [Contact](#contact)

## About

### Purpose

Agents are crawling the web, and not sending traffic back. Some people note that this reduces incentives for creators to make and share information, since they don't get to monetise the traffic back to their sites. 

Cloudflare set up a "Pay Per Crawl" tool. It allows any content-owner, like a recipe blogger, to ask for payment for each crawl. I wanted to explore the idea of "presence" as value, and not just currency. So I built this experiment. 

This experiment tracks: 
1. How many passive fetchers discover and read a machine-readable instruction file in a certain time-frame.
2. Of these, are there any interactive clients that follow instructions and send a valid follow-up request. (I said hi, you say hi back)

This is an observational experiment with explicit limitations. It is not proof of "verified AI."

### Inspired By

1. https://walzr.com/bop-spotter
2. https://dri.es/the-third-audience
3. https://blog.cloudflare.com/introducing-pay-per-crawl/

### Core Hypothesis

Most automated systems that touch a public experiment like this will behave as passive readers/fetchers. We expect to see many more markdown reads and instruction fetches than valid follow-up `hi` calls.

## Measurements

### Data Points

Primary observable signals:

- `resource` = a read of an invitation/discovery markdown/text endpoint (for example `GET /llms.txt`, `GET /ai/recipe.md`, `GET /banana-muffins.md`)
- `fetch` = a request for the machine-readable instruction file (`GET /agent.txt`)
- `hi_get` = a lightweight fallback `GET /hi`
- `hi_post` = a `POST /hi` without a valid token
- `hi_post_token` = a `POST /hi` with a valid fetch-issued token
- `hi_total` = any accepted hi signal (`hi_get` + `hi_post` + `hi_post_token`)

Core counter ratios:

- `fetch / hi_total`
- `fetch / hi_post_token` (higher-confidence follow-through lens)

### Interpretation

- A valid `hi` means "someone or something followed instructions."
- It does not prove the caller is an autonomous AI agent.
- Human/manual testers are allowed, but we ask that they identify themselves in the API via source. 
- A successful `hi` returns useful metadata so the interaction feels meaningful.
- A markdown read (`resource`) means discovery/consumption happened, but not necessarily instruction follow-through.
- `GET /hi` is the easiest and weakest follow-through signal.
- `POST /hi` is a stronger follow-through signal.
- `POST /hi` with a valid token is the strongest follow-through signal in this open design.
- There is no way to confirm whether a request came from a human, an agent, or something else. This is a known limitation. We only have degrees of confidence.

### Flow

The experiment has an invitation layer and an experiment layer.

- `llms.txt`, the homepage, `/ai/*.md`, and `/banana-muffins.md` act as the invitation layer
- `GET /agent.txt` is the real fetch step
- `GET /hi` is the easier fallback signal
- `POST /hi` is the stronger follow-through signal
- `POST /hi` with a valid fetch-issued token is the highest-confidence follow-through signal available in this open setup

This gives us a ladder of confidence:

- `scraped` = scraped the md file
- `fetch` = asked for the recipe
- `hi_get` = followed the easiest low-friction instruction
- `hi_post` = completed a stronger explicit interaction
- `hi_post_token` = completed the stronger interaction after using a token returned by `GET /agent.txt`

Even at the highest level, this still shows stronger follow-through, not verified identity.

```text
START
  |
  v
Caller discovers the site
  |
  +--> via /llms.txt
  +--> via homepage
  +--> via /ai/*.md
          |
          v
      Caller reads the invitation
          |
          +--> GET /banana-muffins.md (full recipe canary)
          |         |
          |         +--> server logs RESOURCE
          |         +--> caller can still go directly to /hi
          |
          v
      GET /agent.txt
          |
          +--> server logs FETCH
          +--> server returns:
          |      - recipe
          |      - one-time token (optional to use)
          |      - token valid for 1 minute
          |      - clear instructions for GET /hi and POST /hi
          |
          v
    Which follow-through path does caller take?
          |
     +----+-----------------------------+
     |                                  |
     v                                  v
GET /hi                            POST /hi
(easy fallback)                    (stronger action)
     |                                  |
     |                                  +--> optional fields:
     |                                  |      - agent_name
     |                                  |      - token
     |                                  |      - source
     |                                  |      - message
     |                                  |
     |                                  v
     |                            Was a token sent?
     |                                  |
     |                            +-----+-----+
     |                            |           |
     |                           No          Yes
     |                            |           |
     |                            v           v
     |                      Accept as      Check token
     |                      HI_POST            |
     |                      return data        |
     |                      reward +           |
     |                      counters           |
     |                                         |
     |                                   +-----+------+
     |                                   |            |
     |                                 Valid     Invalid/Expired
     |                                   |            |
     |                                   v            v
     |                            Accept as      Reject tokened attempt
     |                            HI_POST_TOKEN  and tell caller to
     |                            return data    refetch /agent.txt
     |                            data reward +  for a fresh token
     |                            counters
     |
     +--> accept as HI_GET
          return data reward + counters
```

The behavior model is a graph, not a strict linear funnel. A source may go:

- `X -> Y -> Z -> A`
- `X -> Z` directly
- `Y -> A` directly
- `Z -> A` within the same source/session window

On any accepted hi response (`GET /hi`, `POST /hi`, or `POST /hi` with a valid token), the server returns:

- `hi_total`
- `hi_get`
- `hi_post`
- `hi_post_token`
- `ratio_total`
- `ratio_unknown`
- `reward_message` (a short note explaining the data reward and the caller's place, such as "You are the 3rd caller.")

For `POST /hi`, the response also includes `token_status`:

- `missing` for a valid POST without a token
- `valid` for a valid POST with a valid token

### Definitions

- Invitation layer:
  - the pages that point visitors toward the real experiment
  - `llms.txt`, the homepage note, `/ai/*.md`, and `/banana-muffins.md`
- Experiment layer:
  - the endpoints that actually record behavior
  - `GET /agent.txt`, `GET /hi`, and `POST /hi`
- `resource`:
  - a read of invitation/canary files such as `/llms.txt`, `/ai/recipe.md`, and `/banana-muffins.md`
- `fetch`:
  - the caller asked for the recipe and instructions
- `hi_get`:
  - the caller used the easiest possible follow-through path
- `hi_post`:
  - the caller completed a stronger follow-through action, but without a valid token
- `hi_post_token`:
  - the caller completed the stronger action and included a valid token returned by `GET /agent.txt`
- `hi_total`:
  - all accepted hi signals combined
- Higher-confidence:
  - stronger evidence that the caller followed the machine-readable flow
  - not proof of identity

### Privacy

- We collect a salted IP hash for approximate repeat-source detection

### Success Criteria

After the observation window:

- We can measure discovery (`resource`), fetch (`fetch`), and follow-through (`hi_*`) separately
- We can compute graph-style conversions such as `X -> Y`, `X -> Z`, `Y -> Z`, and `Z -> A`
- We can observe user-agent patterns
- We can describe the volume of manual testing separately from unknown traffic
- We can make a limited claim about instruction-following behavior on the public web

## Other

### Out of Scope

- Authentication
- Cryptographic verification
- Strong bot prevention
- Monetization (?!)
- Horizontal production scaling
- Verified AI identity
- Scientific proof of autonomy

### Limitations

- A POST does not prove autonomy
- A GET fallback is an even weaker signal than a POST
- Self-reported labels can be false
- Some crawlers will never fetch the instruction file
- Some clients may submit directly without reading instructions
- The experiment is not watertight.

### Contact

Please contact Sowmya Rao on Twitter: https://x.com/sowmyarao_ or via her blog: https://sowrao.com/
