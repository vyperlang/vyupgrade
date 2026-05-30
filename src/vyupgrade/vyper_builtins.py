from __future__ import annotations


INTERFACE_ALIASES = (
    ("IERC20", "ERC20"),
    ("IERC20Detailed", "ERC20Detailed"),
    ("IERC165", "ERC165"),
    ("IERC721", "ERC721"),
    ("IERC721Metadata", "ERC721Metadata"),
    ("IERC721Enumerable", "ERC721Enumerable"),
    ("IERC1155", "ERC1155"),
    ("IERC4626", "ERC4626"),
)


def _with_interface_aliases(
    entries: dict[str, dict], aliases: tuple[tuple[str, str], ...] = INTERFACE_ALIASES
) -> dict[str, dict]:
    expanded = dict(entries)
    for alias, canonical in aliases:
        if canonical in expanded:
            expanded[alias] = expanded[canonical]
    return expanded


BUILTIN_INTERFACES = _with_interface_aliases(
    {
        "ERC20": {
            "totalSupply": "view",
            "balanceOf": "view",
            "allowance": "view",
            "transfer": "nonpayable",
            "transferFrom": "nonpayable",
            "approve": "nonpayable",
        },
        "ERC20Detailed": {"name": "view", "symbol": "view", "decimals": "view"},
        "ERC165": {"supportsInterface": "view"},
        "ERC721": {
            "balanceOf": "view",
            "ownerOf": "view",
            "safeTransferFrom": "nonpayable",
            "transferFrom": "nonpayable",
            "approve": "nonpayable",
            "setApprovalForAll": "nonpayable",
            "getApproved": "view",
            "isApprovedForAll": "view",
        },
        "ERC721Metadata": {"name": "view", "symbol": "view", "tokenURI": "view"},
        "ERC721Enumerable": {
            "totalSupply": "view",
            "tokenOfOwnerByIndex": "view",
            "tokenByIndex": "view",
        },
        "ERC1155": {
            "balanceOf": "view",
            "balanceOfBatch": "view",
            "setApprovalForAll": "nonpayable",
            "isApprovedForAll": "view",
            "safeTransferFrom": "nonpayable",
            "safeBatchTransferFrom": "nonpayable",
        },
        "ERC4626": {
            "asset": "view",
            "totalAssets": "view",
            "convertToShares": "view",
            "convertToAssets": "view",
            "maxDeposit": "view",
            "previewDeposit": "view",
            "deposit": "nonpayable",
            "maxMint": "view",
            "previewMint": "view",
            "mint": "nonpayable",
            "maxWithdraw": "view",
            "previewWithdraw": "view",
            "withdraw": "nonpayable",
            "maxRedeem": "view",
            "previewRedeem": "view",
            "redeem": "nonpayable",
        },
    }
)

BUILTIN_INTERFACE_RETURNS = _with_interface_aliases(
    {
        "ERC20": {
            "totalSupply": "uint256",
            "balanceOf": "uint256",
            "allowance": "uint256",
            "transfer": "bool",
            "transferFrom": "bool",
            "approve": "bool",
        },
        "ERC20Detailed": {"name": "String[64]", "symbol": "String[32]", "decimals": "uint8"},
        "ERC165": {"supportsInterface": "bool"},
        "ERC721": {
            "balanceOf": "uint256",
            "ownerOf": "address",
            "getApproved": "address",
            "isApprovedForAll": "bool",
        },
        "ERC721Metadata": {"name": "String[64]", "symbol": "String[32]", "tokenURI": "String[256]"},
        "ERC721Enumerable": {
            "totalSupply": "uint256",
            "tokenOfOwnerByIndex": "uint256",
            "tokenByIndex": "uint256",
        },
        "ERC1155": {
            "balanceOf": "uint256",
            "balanceOfBatch": "DynArray[uint256, 1024]",
            "isApprovedForAll": "bool",
        },
        "ERC4626": {
            "asset": "address",
            "totalAssets": "uint256",
            "convertToShares": "uint256",
            "convertToAssets": "uint256",
            "maxDeposit": "uint256",
            "previewDeposit": "uint256",
            "deposit": "uint256",
            "maxMint": "uint256",
            "previewMint": "uint256",
            "mint": "uint256",
            "maxWithdraw": "uint256",
            "previewWithdraw": "uint256",
            "withdraw": "uint256",
            "maxRedeem": "uint256",
            "previewRedeem": "uint256",
            "redeem": "uint256",
        },
    }
)

BUILTIN_INTERFACE_PARAMS = _with_interface_aliases(
    {
        "ERC20": {
            "balanceOf": {"owner": "address"},
            "allowance": {"owner": "address", "spender": "address"},
            "transfer": {"to": "address", "amount": "uint256"},
            "transferFrom": {"owner": "address", "to": "address", "amount": "uint256"},
            "approve": {"spender": "address", "amount": "uint256"},
        },
        "ERC165": {"supportsInterface": {"interface_id": "bytes4"}},
        "ERC721": {
            "balanceOf": {"owner": "address"},
            "ownerOf": {"tokenId": "uint256"},
            "safeTransferFrom": {"owner": "address", "to": "address", "tokenId": "uint256"},
            "transferFrom": {"owner": "address", "to": "address", "tokenId": "uint256"},
            "approve": {"to": "address", "tokenId": "uint256"},
            "setApprovalForAll": {"operator": "address", "approved": "bool"},
            "getApproved": {"tokenId": "uint256"},
            "isApprovedForAll": {"owner": "address", "operator": "address"},
        },
        "ERC721Metadata": {"tokenURI": {"tokenId": "uint256"}},
        "ERC721Enumerable": {
            "tokenOfOwnerByIndex": {"owner": "address", "index": "uint256"},
            "tokenByIndex": {"index": "uint256"},
        },
        "ERC1155": {
            "balanceOf": {"account": "address", "id": "uint256"},
            "setApprovalForAll": {"operator": "address", "approved": "bool"},
            "isApprovedForAll": {"account": "address", "operator": "address"},
            "safeTransferFrom": {
                "owner": "address",
                "to": "address",
                "id": "uint256",
                "amount": "uint256",
            },
        },
    }
)
