package games.dominion.actions;

import core.AbstractGameState;
import core.actions.AbstractAction;
import core.components.Card;
import games.dominion.DominionConstants;
import games.dominion.DominionGame;
import games.dominion.DominionGameState;
import games.dominion.cards.CardType;
import games.dominion.cards.DominionCard;

import java.util.Objects;
import java.util.Optional;
import java.util.stream.Collectors;

import static games.dominion.DominionConstants.*;

public class DiscardCard extends AbstractAction {

    final int player;
    final CardType type;

    public DiscardCard(CardType type, int playerId) {
        this.type = type;
        player = playerId;
    }

    @Override
    public boolean execute(AbstractGameState ags) {
        DominionGameState state = (DominionGameState) ags;
        Optional<DominionCard> cardToDiscard = state.getDeck(DeckType.HAND, player).stream()
                .filter(card -> card.cardType() == this.type).findFirst();

        if (cardToDiscard.isPresent()) {
            DominionCard card = cardToDiscard.get();
            state.moveCard(card, player, DeckType.HAND, player, DeckType.DISCARD);
        } else {
            System.out.println(this);
            System.out.println("Hand: " + state.getDeck(DeckType.HAND, player).stream().map(Card::toString).collect(Collectors.joining()));
            System.out.printf("Deck size: %d, Discard size: %d%n", state.getDeck(DeckType.DRAW, player).getSize(), state.getDeck(DeckType.DISCARD, player).getSize());
            int historyLength = state.getHistoryAsText().size();
            System.out.println(state.getHistoryAsText().subList(historyLength - 10, historyLength).stream().map(Objects::toString).collect(Collectors.joining("\n")));
            throw new AssertionError("Cannot discard card that is not in hand : " + type);
        }
        return true;
    }


    /**
     * Create a copy of this action, with all of its variables.
     * NO REFERENCES TO COMPONENTS TO BE KEPT IN ACTIONS, PRIMITIVE TYPES ONLY.
     *
     * @return - new AbstractAction object with the same properties.
     */
    @Override
    public AbstractAction copy() {
        return this;
        // no state
    }

    @Override
    public String getString(AbstractGameState state) {
        return String.format("Player %d discards %s", player, type);
    }

    @Override
    public boolean equals(Object other) {
        if (other instanceof DiscardCard) {
            DiscardCard dc = (DiscardCard) other;
            return dc.player == player && dc.type == type;
        }
        return false;
    }

    @Override
    public int hashCode() {
        return Objects.hash(player, type);
    }
}
